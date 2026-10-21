"""Loopback-only control panel; a single background worker owns the phone."""
import copy
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import socket
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit
import webbrowser

from modules.campaign import CampaignRunner, NeedsAttention, RunControl, RunStats, Stopped, load_config, validate_config
from modules.daily_state import DailyState, local_day
from modules.notifications import notify_security_attention
from modules.runtime import data_dir, version
from modules.task_model import external_app_names

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent


class LocalHTTPServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR permits two listeners on the same address. Use an
    # exclusive bind so a second launch cannot create a second device worker.
    allow_reuse_address = False

    def server_bind(self):
        if sys.platform == 'win32' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def discover_devices():
    from modules.adb_transport import AndroidConnectionError, list_android_devices

    devices, errors = [], []
    try:
        for device in list_android_devices():
            devices.append(dict(serial=device['serial'], type='uiautomator2',
                                status=device['state'], model=device['model']))
    except AndroidConnectionError as exc:
        errors.append(f'adb: {exc}')
    # Discovery shares the connection helper's exact Android executable and
    # state parser. It does not perform the connection-only backend restart or
    # wait for authorization; an unauthorized phone remains selectable here.
    for tool, args, kind in [('hdc', ['list', 'targets'], 'hmdriver2')]:
        binary = shutil.which(tool)
        if not binary:
            continue
        try:
            result = subprocess.run([binary, *args], capture_output=True, text=True, timeout=8,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                errors.append(f'{tool}: {result.stderr.strip() or result.stdout.strip()}')
            for line in result.stdout.splitlines():
                fields = line.split()
                if not fields or line.startswith(('List ', '*', '[Empty]')):
                    continue
                if len(fields) == 1:
                    devices.append(dict(serial=fields[0], type=kind, status='device', model='HarmonyOS'))
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f'{tool}: {exc}')
    return dict(devices=devices, errors=errors)


class PanelLogHandler(logging.Handler):
    def __init__(self, controller):
        super().__init__(logging.INFO)
        self.controller = controller

    def emit(self, record):
        if record.name.startswith('modules'):
            self.controller.log(record.levelname, record.getMessage())


class Controller:
    def __init__(self, config=None):
        self.lock = threading.RLock()
        self.device = None
        self.control = RunControl()
        self.thread = None
        self.image = None
        self.sequence = 0
        self.daily = None
        self._daily_lock = threading.RLock()
        self._preview_thread = None
        self._preview_stop = threading.Event()
        self._preview_last = 0.0
        self._round_last_stats = {}
        self._last_failure_stamp = 0.0
        # The same dictionary remains attached to an active runner across a day
        # boundary. Its contents come from this resolved device and local date.
        self.unavailable_tasks = {}
        self.state = dict(status='disconnected', phase='idle', connected=False,
                          message='连接手机后，点击开始执行自动进入活动并识别任务', package='', source='',
                          current_task='', remaining=0, stats=asdict(RunStats()), tasks=[],
                          config=config or load_config(), device={}, logs=[], updated_at='',
                          daily=None, last_failure=None, image_sequence=0, image_updated_at='',
                          cross_app_options=external_app_names())

    def log(self, level, message):
        with self.lock:
            self.sequence += 1
            self.state['logs'].append(dict(id=self.sequence, time=datetime.now().astimezone().isoformat(timespec='seconds'),
                                            level=level, message=message))
            self.state['logs'] = self.state['logs'][-600:]
        with self._daily_lock:
            if self.daily:
                self.daily.log(level, message)

    def update(self, **event):
        with self.lock:
            self.state.update(event)
            self.state['updated_at'] = datetime.now().astimezone().isoformat(timespec='seconds')

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def on_image(self, image):
        preview = image.convert('RGB').copy()
        preview.thumbnail((540, 1200))
        output = io.BytesIO()
        preview.save(output, format='JPEG', quality=78)
        with self.lock:
            self.image = output.getvalue()
            self.state['image_sequence'] += 1
            self.state['image_updated_at'] = datetime.now().astimezone().isoformat(timespec='milliseconds')
        self._preview_last = time.monotonic()

    def _ensure_daily(self):
        """Called outside controller.lock; bind only after the adapter resolves serial."""
        with self._daily_lock:
            device = self.device
            if device is None:
                return False
            date = local_day()
            identity = getattr(device, 'identity_serial', None)
            if not isinstance(identity, str) or not identity:
                identity = device.serial
            if (self.daily is not None and self.daily.date == date
                    and self.daily.serial == identity and self.daily.device_type == device.device_type):
                self.daily.redaction_values.add(device.serial)
                return False
            self.daily = DailyState(data_dir(), identity, device.device_type, date)
            self.daily.redaction_values.add(device.serial)
            self.unavailable_tasks.clear()
            self.unavailable_tasks.update(self.daily.unavailable_tasks())
            self.update(daily=self.daily.snapshot())
            if self.daily.recovery_warning:
                self.log('WARNING', self.daily.recovery_warning)
            return True

    def _summary_state(self):
        with self.lock:
            return {key: copy.deepcopy(self.state[key]) for key in
                    ('status', 'phase', 'message', 'current_task', 'remaining', 'package', 'updated_at')}

    def _persist_state(self):
        self._ensure_daily()
        with self._daily_lock:
            if self.daily:
                self.daily.save_state(self._summary_state(), self.unavailable_tasks)
                self.update(daily=self.daily.snapshot())

    def _runner_event(self, session_id, count_stats, **event):
        previous = self._round_last_stats.get(session_id, {})
        self._ensure_daily()
        self.update(**event)
        with self._daily_lock:
            if self.daily:
                summary = self.daily.progress(
                    session_id, event.get('stats', previous), tasks=event.get('tasks'),
                    unavailable_tasks=self.unavailable_tasks, count_stats=count_stats,
                    # A pause/diagnostic export may have switched stores before
                    # this event. The store uses its own watermark when present.
                    baseline=previous, state=self._summary_state())
                self.update(daily=summary)
        if 'stats' in event:
            self._round_last_stats[session_id] = copy.deepcopy(event['stats'])

    def on_failure(self, event=None, **details):
        """Campaign callback: retain evidence without taking the panel state lock."""
        event = dict(event or {}, **details)
        image = event.pop('image', None)
        with self.lock:
            previous = self.state['last_failure']
        if (previous and previous.get('reason') == event.get('reason')
                and time.monotonic() - self._last_failure_stamp < 3
                and (not event.get('task') or previous.get('task') == event.get('task'))):
            # The runner reports a guard with task/phase evidence before raising
            # NeedsAttention. The outer exception handler must not save it twice.
            return
        self._ensure_daily()
        if not self.daily:
            return
        if image is None and self.device is not None:
            try:
                image = self.device.screenshot()
            except Exception as exc:
                event['capture_error'] = f'{type(exc).__name__}: {exc}'
        with self._daily_lock:
            evidence = self.daily.failure(event, image)
            self.update(last_failure=evidence, daily=self.daily.snapshot())
            self._last_failure_stamp = time.monotonic()

    def _preview_loop(self):
        while not self._preview_stop.wait(.05):
            if time.monotonic() - self._preview_last < .35:
                continue
            with self.lock:
                device = self.device
            if device is None:
                continue
            io_lock = getattr(device, 'io_lock', None)
            if io_lock is None or not io_lock.acquire(blocking=False):
                continue
            try:
                image = device.screenshot(max_age=.15)
                if self.device is device:
                    self.on_image(image)
            except Exception:
                # A preview failure does not interrupt an ongoing recovery.
                self._preview_last = time.monotonic()
                logger.debug('后台截图预览失败', exc_info=True)
            finally:
                io_lock.release()

    def _start_preview(self):
        if self._preview_thread and self._preview_thread.is_alive():
            return
        self._preview_thread = threading.Thread(target=self._preview_loop, daemon=True,
                                                name='miao-preview-worker')
        self._preview_thread.start()

    def diagnostics(self):
        if self.device is None:
            raise ValueError('请先连接设备，再导出当天诊断记录')
        self._persist_state()
        with self._daily_lock:
            if not self.daily:
                raise ValueError('请先连接设备，再导出当天诊断记录')
            return self.daily.diagnostics(self.snapshot())

    def _connection_status(self, message):
        self.control.check()
        self.update(status='connecting', phase='connecting', message=str(message))
        self.log('INFO', str(message))

    def dispatch(self, action, payload):
        control_action = action in ('pause', 'resume', 'stop')
        with self.lock:
            if control_action:
                if not (self.thread and self.thread.is_alive()):
                    raise ValueError('当前没有正在执行的操作')
                if action == 'pause':
                    if self.state['status'] != 'running':
                        raise ValueError('只有执行任务时可以暂停')
                    self.control.paused.set()
                    self.update(status='paused', message='已暂停；可以手动操作手机，再继续执行')
                elif action == 'resume':
                    if self.state['status'] != 'paused':
                        raise ValueError('当前任务未暂停')
                    self.control.paused.clear()
                    self.update(status='running', message='继续执行')
                else:
                    self.control.stopped.set()
                    self.update(status='stopping', message='正在停止，当前设备调用返回后结束')
            else:
                if self.thread and self.thread.is_alive():
                    raise ValueError('设备正在执行操作，请先停止当前任务')
                if action not in ('connect', 'scan', 'run', 'capture', 'configure'):
                    raise ValueError('未知操作')
                if action not in ('connect', 'configure') and self.device is None:
                    raise ValueError('请先连接设备')
                config = copy.deepcopy(self.state['config'])
                overrides = payload.get('config', {})
                if not isinstance(overrides, dict) or set(overrides) - set(config):
                    raise ValueError('配置字段无效')
                config.update(overrides)
                validate_config(config)
                if action == 'configure':
                    self.update(config=config)
                else:
                    self.control = RunControl()
                    self.update(config=config, status={'connect': 'connecting', 'scan': 'scanning',
                                                      'run': 'running', 'capture': 'scanning'}[action],
                                message='正在连接手机' if action == 'connect' else '正在读取手机页面')
                    self.thread = threading.Thread(target=self._work, args=(action, payload, config), daemon=True,
                                                   name='miao-device-worker')
                    self.thread.start()
        # Disk and device operations must never run under the UI state lock.
        if control_action:
            self.log('INFO', {'pause': '用户暂停', 'resume': '用户继续', 'stop': '用户停止'}[action])
            self._persist_state()
        elif action == 'configure':
            self.log('INFO', '执行设置已应用')

    def _work(self, action, payload, config):
        self._last_failure_stamp = 0.0
        try:
            if action == 'connect':
                from modules.device_adapter import DeviceAdapter
                # A failed attempt to choose another phone must not leave the
                # previous phone looking connected or export its diagnostics.
                self.device = None
                with self._daily_lock:
                    self.daily = None
                    self.unavailable_tasks.clear()
                with self.lock:
                    self.image = None
                    self.state['image_sequence'] += 1
                    self.state['image_updated_at'] = ''
                    self.state['logs'] = []
                self.update(connected=False, device={}, daily=None, last_failure=None,
                            package='', source='', tasks=[], stats=asdict(RunStats()),
                            current_task='', remaining=0, phase='connecting')
                kind = payload.get('device_type', 'uiautomator2')
                serial = payload.get('serial') or None
                if kind not in ('uiautomator2', 'hmdriver2') or (serial and (not isinstance(serial, str) or len(serial) > 128)):
                    raise ValueError('设备类型或序列号无效')
                device = DeviceAdapter(kind, serial, on_status=self._connection_status)
                info = device.current_app()
                self.control.check()
                self.on_image(device.screenshot())
                self.device = device
                device.preview_managed = True
                self._ensure_daily()
                geometry = ''
                try:
                    width, height = device.display_size
                    geometry = f'（{width}×{height}，{"横屏" if width > height else "竖屏"}）'
                except Exception:
                    logger.debug('读取设备屏幕尺寸失败', exc_info=True)
                self.update(connected=True, device=dict(type=kind, serial=device.serial or '自动选择'),
                            package=info.get('package', ''), status='ready', phase='ready',
                            tasks=[], message=f'设备已连接{geometry}，点击开始执行将自动打开活动并识别任务')
                self._start_preview()
                self.log('INFO', f'设备连接成功：{device}')
            elif action == 'capture':
                self.control.check()
                self.on_image(self.device.screenshot())
                self.update(status='ready', message='手机画面已刷新')
            else:
                self._ensure_daily()
                session_id = uuid.uuid4().hex
                self._round_last_stats[session_id] = {}
                self.update(stats=asdict(RunStats()))
                runner = CampaignRunner(
                    self.device, config, self.control,
                    lambda **event: self._runner_event(session_id, action == 'run', **event), self.on_image,
                    unavailable_tasks=self.unavailable_tasks, on_failure=self.on_failure)
                auto_open = payload.get('auto_open', True)
                if not isinstance(auto_open, bool):
                    raise ValueError('auto_open 必须为布尔值')
                self.log('INFO', '开始识别任务' if action == 'scan' else '开始执行任务')
                if action == 'scan':
                    runner.scan(auto_open=auto_open)
                    self.update(status='ready')
                else:
                    runner.run(auto_open=auto_open)
                    self.update(status='finished')
                    self._save_report()
        except Stopped:
            self.interrupt_records('已停止，本次进度尚未核验')
            self.update(status='stopped', phase='stopped', message='已停止，可从当前页面重新开始', remaining=0)
        except NeedsAttention as exc:
            self.interrupt_records('需要手动处理后重新核验')
            self.log('WARNING', str(exc))
            self.update(status='needs_attention', phase='needs_attention', message=str(exc), remaining=0)
            if not exc.evidence_saved:
                self.on_failure(dict(reason=str(exc), phase='needs_attention', kind=exc.kind))
            if exc.kind == 'security':
                notify_security_attention()
        except Exception as exc:
            self.interrupt_records('执行中断，本次进度尚未核验')
            logger.exception('设备操作失败')
            self.update(status='error', phase='connect_failed' if action == 'connect' else 'error',
                        message=f'{type(exc).__name__}: {exc}', remaining=0)
            self.on_failure(dict(reason=f'{type(exc).__name__}: {exc}', phase='error', kind='exception'))
        finally:
            if action in ('run', 'scan') and self.device is not None and not self.control.stopped.is_set():
                try:
                    self.on_image(self.device.screenshot())
                except Exception:
                    logger.debug('刷新预览失败', exc_info=True)
            if self.device is not None:
                try:
                    self._persist_state()
                    self._save_report()
                except Exception:
                    logger.exception('保存本地当天记录失败')

    def interrupt_records(self, reason):
        with self.lock:
            for task in self.state['tasks']:
                if task.get('status') == 'running':
                    task.update(status='pending', reason=reason)

    def _save_report(self):
        folder = data_dir()
        report = self.snapshot()
        report.pop('config', None)
        from modules.daily_state import _atomic_json
        _atomic_json(folder / 'last-run.json', report)


def make_handler(controller, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def allowed_host(self):
            return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}

        def reply(self, code, body, content_type='application/json; charset=utf-8', extra=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.allowed_host():
                self.reply(403, {'error': '只允许本机访问'})
                return
            path = urlsplit(self.path).path
            if path == '/api/state':
                self.reply(200, dict(controller.snapshot(), token=token, version=version(),
                                     busy=bool(controller.thread and controller.thread.is_alive())))
            elif path == '/api/health':
                self.reply(200, dict(app='miao-task', version=version()))
            elif path == '/api/devices':
                self.reply(200, discover_devices())
            elif path == '/api/screenshot':
                with controller.lock:
                    data = controller.image
                self.reply(200 if data else 404, data or b'', 'image/jpeg')
            elif path == '/api/report':
                self.reply(200, controller.snapshot(), extra={'Content-Disposition': 'attachment; filename="double11-report.json"'})
            elif path == '/api/diagnostics':
                try:
                    self.reply(200, controller.diagnostics(), 'application/zip',
                               extra={'Content-Disposition': 'attachment; filename="miao-diagnostics.zip"'})
                except ValueError as exc:
                    self.reply(400, {'error': str(exc)})
                except Exception:
                    logger.exception('导出当天诊断记录失败')
                    self.reply(500, {'error': '无法导出当天诊断记录，请检查本地数据目录是否可写'})
            elif path in ('/', '/app.js', '/style.css'):
                name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[path]
                base = Path(getattr(sys, '_MEIPASS', ROOT)) / 'web'
                mime = {'/': 'text/html; charset=utf-8', '/app.js': 'text/javascript; charset=utf-8', '/style.css': 'text/css; charset=utf-8'}[path]
                self.reply(200, (base / name).read_bytes(), mime)
            else:
                self.reply(404, {'error': '路径不存在'})

        def do_POST(self):
            origin = self.headers.get('Origin')
            expected = f'http://{self.headers.get("Host", "")}'
            if (not self.allowed_host() or self.headers.get('X-Miao-Token') != token or
                    (origin and origin != expected) or self.headers.get('Sec-Fetch-Site') == 'cross-site'):
                self.reply(403, {'error': '请求来源校验失败，请刷新本地控制台'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 32768:
                    raise ValueError('请求长度无效')
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError('请求必须为 JSON 对象')
                path = urlsplit(self.path).path
                if not path.startswith('/api/'):
                    self.reply(404, {'error': '路径不存在'})
                    return
                if path == '/api/shutdown':
                    controller.control.stopped.set()
                    controller._preview_stop.set()
                    self.reply(202, {'ok': True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                controller.dispatch(path.removeprefix('/api/'), payload)
                self.reply(202, {'ok': True})
            except (ValueError, TypeError) as exc:
                self.reply(400, {'error': str(exc)})
            except Exception:
                logger.exception('控制台请求失败')
                self.reply(500, {'error': '请求失败，请查看运行日志'})
    return Handler


def serve(port=8765, open_browser=True, config=None, port_file=None):
    controller = Controller(config)
    logging.getLogger().addHandler(PanelLogHandler(controller))
    handler = make_handler(controller, secrets.token_urlsafe(32))
    def existing_assistant():
        if not port:
            return False
        import urllib.request
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=1) as response:
                existing = json.load(response)
            return isinstance(existing, dict) and existing.get('app') == 'miao-task'
        except (OSError, ValueError):
            return False
    if existing_assistant():
        if open_browser:
            webbrowser.open(f'http://127.0.0.1:{port}')
        return controller
    try:
        server = LocalHTTPServer(('127.0.0.1', port), handler)
    except OSError as exc:
        if exc.errno not in (48, 98, 10048, 10013) and getattr(exc, 'winerror', None) not in (10048, 10013):
            raise
        # Double-clicking again opens the existing assistant, never another device worker.
        if existing_assistant():
            if open_browser:
                webbrowser.open(f'http://127.0.0.1:{port}')
            return controller
        server = LocalHTTPServer(('127.0.0.1', 0), handler)
    url = f'http://127.0.0.1:{server.server_port}'
    if port_file:
        Path(port_file).write_text(json.dumps({'url': url}), encoding='utf-8')
    if sys.stdout is not None:
        print(f'双11任务助手已启动：{url}\n按 Ctrl+C 退出。')
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:
        controller.control.stopped.set()
    finally:
        controller._preview_stop.set()
        try:
            controller._persist_state()
        except Exception:
            logger.exception('关闭控制台时保存当天记录失败')
        server.server_close()
    return controller
