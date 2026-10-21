"""Bounded, observable task runner for Double 11 and configurable campaigns."""
from collections import Counter
from dataclasses import asdict, dataclass
import json
import hashlib
import logging
from pathlib import Path
import re
import threading
import time

from modules.screen_reader import Screen, ScreenReader
from modules.task_model import CLAIM_ACTIONS, TextNode, bounds_of, is_task_list, normalize, parse_tasks

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent


def load_config(path=None):
    import sys
    base = Path(getattr(sys, '_MEIPASS', ROOT))
    config = json.loads((base / 'configs' / 'double11.json').read_text(encoding='utf-8'))
    if path:
        config.update(json.loads(Path(path).read_text(encoding='utf-8')))
    return validate_config(config)


def validate_config(config):
    limits = {
        'default_browse_seconds': (1, 120), 'visit_seconds': (1, 120),
        'browse_extra_seconds': (0, 60),
        'max_task_seconds': (5, 180), 'settle_seconds': (0, 10),
        'page_timeout': (3, 60), 'return_timeout': (5, 90),
        'max_back_steps': (1, 8), 'max_attempts': (1, 3),
        'max_tasks': (1, 300), 'max_scroll_pages': (1, 20),
    }
    for key, (low, high) in limits.items():
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not low <= value <= high:
            raise ValueError(f'{key} 必须在 {low}～{high} 之间')
        if key.startswith('max_') and key not in ('max_task_seconds',) and not isinstance(value, int):
            raise ValueError(f'{key} 必须是整数')
    for key in ('allow_cross_app', 'ocr', 'scroll_browse', 'claim_rewards'):
        if not isinstance(config.get(key), bool):
            raise ValueError(f'{key} 必须为布尔值')
    kinds = config.get('enabled_task_kinds', ['browse', 'search', 'visit', 'claim'])
    if (not isinstance(kinds, list) or not kinds
            or any(not isinstance(kind, str) or kind not in {'browse', 'search', 'visit', 'claim'} for kind in kinds)
            or len(kinds) != len(set(kinds))):
        raise ValueError('本轮任务类型至少选择一项，且只能包含浏览、搜索、跳转和领奖')
    config['enabled_task_kinds'] = kinds
    if not isinstance(config.get('search_keyword'), str) or len(config['search_keyword']) > 120:
        raise ValueError('活动搜索词过长或格式错误')
    query = config.setdefault('task_search_keyword', '手机壳')
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 40:
        raise ValueError('任务搜索词必须为 1～40 个字符')
    allowlist = config.get('allowed_cross_apps')
    if allowlist is not None:
        if (not isinstance(allowlist, list)
                or any(not isinstance(name, str) or not name.strip() or len(name) > 40 for name in allowlist)):
            raise ValueError('跨应用范围必须为应用名称列表')
        names = sorted(dict.fromkeys(name.strip() for name in allowlist))
        # No selection means "every recognized app", not "no app at all".
        config['allowed_cross_apps'] = names or None
    for key in ('entry_labels', 'task_entry_labels'):
        labels = config.get(key)
        if not isinstance(labels, list) or not labels or not all(isinstance(v, str) and 0 < len(v) < 80 for v in labels):
            raise ValueError(f'{key} 必须为非空文字列表')
    return config


class Stopped(Exception):
    pass


class NeedsAttention(Exception):
    def __init__(self, message, *, kind='attention', evidence_saved=False):
        super().__init__(message)
        self.kind = kind
        self.evidence_saved = evidence_saved


class TaskUnavailable(Exception):
    """A terminal condition for this task, rather than a reason to retry it."""
    pass


class RunControl:
    def __init__(self):
        self.stopped = threading.Event()
        self.paused = threading.Event()
        self._paused_seconds = 0.0

    def check(self):
        if self.paused.is_set():
            started = time.monotonic()
            try:
                while self.paused.is_set():
                    if self.stopped.wait(.15):
                        raise Stopped()
            finally:
                self._paused_seconds += time.monotonic() - started
        if self.stopped.is_set():
            raise Stopped()

    def now(self):
        return time.monotonic() - self._paused_seconds

    def wait(self, seconds):
        remaining = max(0, seconds)
        while remaining > 0:
            self.check()
            chunk = min(.15, remaining)
            start = time.monotonic()
            if self.stopped.wait(chunk):
                raise Stopped()
            remaining -= time.monotonic() - start


@dataclass
class RunStats:
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    claims: int = 0
    cross_app: int = 0
    attempts: int = 0
    elapsed: int = 0


class CampaignRunner:
    def __init__(self, device, config=None, control=None, emit=None, on_image=None, unavailable_tasks=None,
                 on_failure=None):
        self.device = device
        self.config = config or load_config()
        self.control = control or RunControl()
        self.emit = emit or (lambda **event: None)
        self.reader = ScreenReader(device, self.config['ocr'], on_image=on_image)
        self.stats = RunStats()
        self.failures = Counter()
        self.records = {}
        self.origin = None
        self.started = time.monotonic()
        self._external_seen = set()
        self._known_tasks = {}
        self._asset_keys = {}
        self.unavailable_tasks = unavailable_tasks if unavailable_tasks is not None else {}
        self._installed_checked = False
        self._installed_packages = None
        self.on_failure = on_failure
        self._active_task = None
        self._phase = 'locating'
        self._task_restart_used = False

    def report_failure(self, reason, task=None, phase=None):
        """Capture evidence at the failing page, before recovery changes it."""
        if self.on_failure:
            try:
                self.on_failure(reason=reason, task=(task or self._active_task).to_dict()
                                if (task or self._active_task) else None,
                                phase=phase or self._phase)
                return True
            except Exception:
                logger.warning('保存失败现场时发生异常', exc_info=True)
        return False

    @staticmethod
    def unavailable_key(task):
        return 'app:' + task.target_app if task.target_app else 'task:' + normalize(task.title)

    def cross_app_blocked(self, task):
        """A per-round choice of which external apps may be visited.

        Unlike an unavailable app this never records a device fact, so the
        task returns as soon as the selection changes.
        """
        allowed = self.config.get('allowed_cross_apps')
        if not task.target_app or not allowed or not self.config['allow_cross_app']:
            return ''
        if task.target_app in allowed:
            return ''
        return f'本轮未选择{task.target_app}的跳转任务；在跨应用范围勾选后可再次执行'

    def availability_reason(self, task, check_installed=False):
        if task.kind in ('manual', 'done', 'claim'):
            return ''
        key = self.unavailable_key(task)
        saved_reason = self.unavailable_tasks.get(key, '')
        if (task.target_packages and self.device.device_type == 'uiautomator2'
                and (check_installed or saved_reason.startswith('未安装'))):
            if not self._installed_checked:
                self._installed_checked = True
                try:
                    packages = self.device.installed_packages()
                    if isinstance(packages, (set, list, tuple)):
                        self._installed_packages = set(packages)
                except Exception:
                    logger.warning('无法读取已安装应用，将依据跳转页面判断', exc_info=True)
            if self._installed_packages is not None:
                if not self._installed_packages.intersection(task.target_packages):
                    reason = f'未安装{task.target_app}，已跳过；安装后重新识别即可恢复'
                    self.unavailable_tasks[key] = reason
                    return reason
                # A new scan refreshes installation state, including after installation.
                if saved_reason.startswith('未安装'):
                    self.unavailable_tasks.pop(key, None)
        return self.unavailable_tasks.get(key, '')

    def check_destination(self, screen):
        if is_task_list(screen.nodes):
            return
        markets = {'com.xiaomi.market', 'com.android.vending', 'com.huawei.appmarket',
                   'com.oppo.market', 'com.heytap.market', 'com.bbk.appstore',
                   'com.tencent.android.qqdownloader'}
        if (screen.package in markets or screen.find(
                r'未安装|尚未安装|没有安装|未找到.{0,8}应用|找不到.{0,8}应用|应用不存在|'
                r'没有可处理|无法.{0,4}(?:打开|跳转)|^(?:立即下载|下载并安装|安装并打开)$')):
            raise TaskUnavailable('目标应用不可用或跳转到安装页，已跳过，不再重复尝试')

    def update(self, phase=None, **data):
        self.stats.elapsed = int(time.monotonic() - self.started)
        self.stats.skipped = sum(r.get('status') in ('manual', 'disabled') for r in self.records.values())
        if phase:
            data['phase'] = phase
            self._phase = phase
        self.emit(stats=asdict(self.stats), **data)

    def read(self, force_ocr=False):
        self.control.check()
        screen = self.reader.read(force_ocr=force_ocr) if force_ocr else self.reader.read()
        self.control.check()
        self.update(package=screen.package, source=screen.source,
                    observation_ms=round(self.reader.last_observation_seconds * 1000))
        return screen

    def click(self, node):
        self.control.check()
        self.device.click(*node.center)

    def tasks(self, screen):
        cache = getattr(screen, '_campaign_tasks', {})
        if id(self) in cache:
            return cache[id(self)]
        tasks = parse_tasks(screen.nodes, screen.width, screen.height)
        used = set()
        for task in tasks:
            if task.key.startswith('milestone-'):
                continue
            center_y = (task.bounds[1] + task.bounds[3]) / 2
            icons = [n for n in screen.nodes if 'O1CN' in n.text
                     and n.bounds[0] < screen.width * .2
                     and abs(n.center[1] - center_y) < screen.height * .04]
            asset = min(icons, key=lambda n: abs(n.center[1] - center_y)).text if icons else ''
            key = self._asset_keys.get(asset) if asset else None
            old_asset_task = self._known_tasks.get(key)
            if old_asset_task and (old_asset_task.total != task.total or old_asset_task.seconds != task.seconds
                                   or (task.total == 1 and old_asset_task.title != task.title)):
                # A completed one-off offer may be replaced in the same slot,
                # even reusing its icon. It must not inherit the old retry budget.
                key = None
            if key in used:
                key = None
            if key is None:
                # Task captions rotate between product names, search and browse.
                # Match existing repeatable rows by their progress contract and
                # geometry; a newly substituted one-off task gets a fresh ID.
                matches = []
                for old in self._known_tasks.values():
                    if old.key in used or old.total != task.total or old.seconds != task.seconds:
                        continue
                    same_title = old.title == task.title
                    close = abs((old.bounds[1] + old.bounds[3]) / 2 - center_y) < screen.height * .04
                    repeatable = (old.total > 1 and old.seconds > 0
                                  and old.kind in ('browse', 'search', 'done')
                                  and task.kind in ('browse', 'search', 'done'))
                    if same_title or (close and repeatable):
                        matches.append((not close, not same_title, abs(old.current - task.current), old.key))
                if matches:
                    key = min(matches)[-1]
            if key is None:
                key = task.key
                if key in used or key in self._known_tasks:
                    key += '-' + hashlib.sha256((asset or str(task.bounds)).encode()).hexdigest()[:6]
            task.key = key
            used.add(key)
            if asset:
                self._asset_keys[asset] = key
            self._known_tasks[key] = task
        cache[id(self)] = tasks
        screen._campaign_tasks = cache
        return tasks

    def remember(self, tasks):
        for task in tasks:
            previous = self.records.get(task.key, {})
            reason = task.skip_reason or self.cross_app_blocked(task) or self.availability_reason(task)
            state = ('done' if task.kind == 'done' else 'disabled' if reason.startswith(('已跳过', '未安装', '目标应用'))
                     else 'manual' if task.skip_reason else 'disabled' if reason else 'pending')
            if previous.get('status') in ('failed', 'disabled') and task.current == previous.get('current'):
                if previous['status'] == 'failed' or reason:
                    state = previous['status']
                    reason = reason or previous.get('reason', '')
            self.records[task.key] = dict(task.to_dict(), status=state,
                                          reason=reason)
        self.stats.skipped = sum(r['status'] in ('manual', 'disabled') for r in self.records.values())
        self.update(tasks=list(self.records.values()))

    def guard(self, screen):
        text = normalize(screen.text)
        if re.search(r'滑块|拖动.{0,8}验证|安全验证|请完成验证|验证码|人机验证|操作过于频繁|账号存在风险|访问被拒绝|'
                     r'将满足描述的所有图片拖放到指定区域', text):
            reason = '页面要求安全验证，自动操作已停止；请在手机上本人处理，完成后点击开始自动执行'
            saved = self.report_failure(reason, phase='security')
            raise NeedsAttention(reason, kind='security', evidence_saved=saved)
        if re.search(r'短信登录|验证码登录|登录即同意|请先登录|立即登录|授权登录|确认付款|支付密码|确认支付', text):
            reason = '页面要求登录、授权或支付，请在手机处理后从当前页面重新开始'
            saved = self.report_failure(reason, phase='attention')
            raise NeedsAttention(reason, evidence_saved=saved)

    def task_panel_entry(self, screen):
        # Controls can say 去赚能量 / 立即赚能量 / 赚能量，按钮. Match short
        # captions containing the configured label, while excluding sheet titles
        # and task descriptions. OCR may split a short caption across two boxes.
        candidates = list(screen.nodes)
        anchor = screen.find(r'^升级赛车拿钱')
        fragments = [n for n in screen.nodes if len(normalize(n.text)) <= 8
                     and any(c in normalize(n.text) for c in '赚能量体力喵币做任务')]
        for first in fragments:
            for second in fragments:
                if first is second:
                    continue
                a, b = first.bounds, second.bounds
                height = max(a[3] - a[1], b[3] - b[1])
                horizontal = (0 <= b[0] - a[2] <= height * 1.5
                              and abs(first.center[1] - second.center[1]) <= height * .6)
                vertical = (0 <= b[1] - a[3] <= height
                            and abs(first.center[0] - second.center[0]) <= max(a[2]-a[0], b[2]-b[0]) * .6)
                if horizontal or vertical:
                    candidates.append(TextNode(first.text + second.text,
                                      (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])),
                                      'ocr-joined'))
        matches = []
        for priority, label in enumerate(self.config['task_entry_labels']):
            # This is the opened sheet's heading, never a button on this campaign.
            if normalize(label) == '做任务赚能量':
                continue
            caption = normalize(label)
            for target in candidates:
                text = re.sub(r'[，,。.:：;；!！>＞()（）\[\]【】\u200b]', '', normalize(target.text))
                if (caption in text and len(text) <= len(caption) + 8
                        and not re.search(r'做任务赚|累计|奖励|浏览|邀请|已完成|去完成|升级|商品|攻略|教程', text)
                        and (text == caption or caption != '做任务')
                        and 0 <= target.center[0] < screen.width and target.center[1] > 0
                        and (target.center[1] < screen.height * .7 or
                             anchor and abs(target.center[1] - anchor.center[1]) < screen.height * .12)
                        and 0 < target.bounds[2] - target.bounds[0] < screen.width * .7
                        and 0 < target.bounds[3] - target.bounds[1] < screen.height * .2):
                    outside_band = bool(anchor and abs(target.center[1] - anchor.center[1]) >= screen.height * .12)
                    matches.append((priority, outside_band, text != caption, len(text), target))
        return min(matches, key=lambda item: item[:4])[4] if matches else None

    def menu_visual_fallback(self, screen):
        """Re-read the visible control band at native resolution, without swipes."""
        anchor = screen.find(r'^升级赛车拿钱')
        if anchor:
            top = max(0, anchor.bounds[1] - round(screen.height * .08))
            bottom = min(screen.height, anchor.bounds[3] + round(screen.height * .08))
        elif self.activity_landing(screen):
            top, bottom = round(screen.height * .25), round(screen.height * .65)
        else:
            return screen
        regions = [(0, top, round(screen.width * .55), bottom),
                   (round(screen.width * .45), top, screen.width, bottom)]
        return self.reader.enrich_regions(screen, regions)

    @staticmethod
    def task_panel_visible(screen):
        return any(normalize(n.text) == '做任务赚能量'
                   and 0 < n.bounds[3] - n.bounds[1] < screen.height * .15
                   and n.center[1] < screen.height * .7 for n in screen.nodes)

    @staticmethod
    def homepage_visible(screen):
        return all(any(normalize(n.text) == label and n.center[1] > screen.height * .8
                       for n in screen.nodes) for label in ('我的淘宝', '购物车'))

    @staticmethod
    def launch_screen(screen):
        return bool(screen.find(r'阿里云提供计算服务') or
                    (screen.find(r'^天猫双') and screen.find(r'^正式开卖$')))

    @staticmethod
    def activity_landing(screen):
        return any(0 < n.bounds[3] - n.bounds[1] < screen.height * .15
                   and ((n.center[1] < screen.height * .18
                         and re.search(r'^淘金币双11|^淘金币双.{0,4}狂享20亿|^淘金币首页$', normalize(n.text)))
                        or (n.center[1] < screen.height * .5 and normalize(n.text) == '升级赛车拿钱'))
                   for n in screen.nodes)

    def enter_task_panel(self, screen, expected_activity=False):
        """Confirm the painted panel; retry a missed menu tap at most twice.

        A late-arriving activity page gets its own bounded rendering window.
        Native controls supplement a missing OCR label only on that visible page.
        Never trust background task rows from an accessibility tree as success.
        """
        deadline = self.control.now() + self.config['page_timeout']
        attempts = 0
        last_tap = self.control.now() - 10
        native_reads = 0
        last_native = self.control.now() - 10
        heading_seen = False
        local_pass = 0
        region_reads = 0
        while True:
            self.guard(screen)
            if self._accept_list(screen):
                return screen
            if not screen.package.startswith('com.taobao.taobao'):
                return screen
            panel_loading = self.task_panel_visible(screen) or bool(screen.find('累计任务奖励', exact=True))
            if panel_loading and not heading_seen:
                heading_seen = True
                # The sheet opens first, then requests the task rows. This is a
                # successful forward transition, deserving its own load window.
                deadline = max(deadline, self.control.now() + self.config['page_timeout'])
                self.update(message='已打开做任务赚能量，等待任务列表加载')
            if self.control.now() >= deadline:
                if not heading_seen and local_pass == 0 and (self.activity_landing(screen) or expected_activity):
                    local_pass = 1
                    deadline = self.control.now() + self.config['page_timeout']
                    attempts = native_reads = region_reads = 0
                    self.update(message='仍在活动页，重新识别按钮并尝试备用点击位置')
                    screen = self.read(force_ocr=bool(self.config['ocr']))
                    continue
                return screen
            target = None if panel_loading else self.task_panel_entry(screen)
            if (not panel_loading and region_reads < 2 and (native_reads or local_pass)
                    and (not target or attempts)):
                region_reads += 1
                screen = self.menu_visual_fallback(screen)
                self.guard(screen)
                target = self.task_panel_entry(screen)
            if (screen.source == 'ocr' and native_reads < 3
                    and (target or self.activity_landing(screen) or expected_activity)
                    and not panel_loading and self.control.now() - last_native >= 1.2):
                native_reads += 1
                last_native = self.control.now()
                native = self.read()
                self.guard(native)
                if native.package == screen.package:
                    if self.task_panel_visible(native):
                        # A sheet may appear during the hierarchy RPC. Confirm
                        # the actual foreground again instead of tapping old OCR.
                        screen = self.read(force_ocr=bool(self.config['ocr']))
                        continue
                    target = self.task_panel_entry(native) or target
            if target and attempts < 3 and self.control.now() - last_tap >= 1.2:
                self.update(message=f'打开赚能量菜单（页面内第{local_pass + 1}轮，第{attempts + 1}/3次）')
                logger.info('任务菜单点击：页面内第%d轮，第%d次，来源%s，文案%s',
                            local_pass + 1, attempts + 1, target.source, target.text)
                if local_pass and target.bounds[3] - target.bounds[1] > screen.height * .035:
                    # An alternate point stays inside a freshly located native
                    # control, rather than guessing a phone-specific coordinate.
                    x = target.center[0]
                    y = target.bounds[1] + round((target.bounds[3] - target.bounds[1]) * .3)
                    self.control.check()
                    self.device.click(x, y)
                else:
                    self.click(target)
                attempts += 1
                last_tap = self.control.now()
            elif not (target or self.activity_landing(screen) or panel_loading or expected_activity or attempts):
                return screen
            self.control.wait(.35)
            screen = self.read(force_ocr=bool(self.config['ocr']))

    def open_task_panel(self, screen):
        if is_task_list(screen.nodes):
            return True
        node = self.task_panel_entry(screen)
        if node:
            self.click(node)
            return True
        return False

    def activity_entry(self, screen, homepage=False):
        """Locate entry words in controls/OCR, independent of icon coordinates."""
        official = self.official_activity_entry(screen)
        if official:
            return official
        for label in self.config['entry_labels']:
            text = normalize(label)
            candidates = [n for n in screen.nodes if 0 < n.bounds[2] - n.bounds[0]
                          and 0 < n.bounds[3] - n.bounds[1] < screen.height * .2
                          and (not homepage or n.center[1] < screen.height * .7)]
            exact = next((n for n in candidates if normalize(n.text) == text), None)
            if exact:
                return exact
            # Accessibility descriptions often append "抵现金" or "按钮".
            # Keep this fallback within homepage functions, outside product cards.
            if homepage:
                matches = [n for n in candidates if text in normalize(n.text)
                           and len(normalize(n.text)) <= 40
                           and not re.search(r'[¥￥]|已售|人付款|商品详情', n.text)]
                if matches:
                    return min(matches, key=lambda n: (len(normalize(n.text)), n.bounds[1]))
        return None

    @staticmethod
    def official_activity_entry(screen):
        # A search result contains TWO exact 淘金币 labels: the search field and
        # the official card. Enter the card's aligned right-hand action instead
        # of reopening the search field or a product advertising an entry.
        descriptions = [n for n in screen.nodes if re.search(r'双11狂享20亿', normalize(n.text))
                        and n.center[1] < screen.height * .35]
        for description in descriptions:
            title = any(normalize(n.text) == '淘金币' and n.center[0] < screen.width * .6
                        and 0 < description.center[1] - n.center[1] < screen.height * .08
                        for n in screen.nodes)
            if title:
                action = next((n for n in screen.nodes if re.fullmatch(r'进入[>›》]?', normalize(n.text))
                               and n.center[0] > screen.width * .65
                               and abs(n.center[1] - description.center[1]) < screen.height * .06), None)
                if action:
                    return action
        return None

    def _accept_list(self, screen):
        if (self.task_panel_visible(screen) and is_task_list(screen.nodes)
                and parse_tasks(screen.nodes, screen.width, screen.height)
                and screen.package.startswith('com.taobao.taobao')):
            self.origin = screen.package
            return True
        return False

    def _restart_taobao(self, package):
        # Count the attempt before issuing RPCs, including failed cold starts.
        self._task_restart_used = True
        self.device.restart_app(package)

    def ensure_awake(self):
        """Dark tablets/phones sleep into unrecognizable frames; wake them.

        A password lockscreen still needs the user; navigation stays bounded
        and the saved failure screenshot shows the lock page.
        """
        try:
            if callable(getattr(self.device, 'is_screen_on', None)) and not self.device.is_screen_on():
                self.update(message='检测到屏幕已熄灭，正在唤醒设备')
                logger.info('屏幕已熄灭，执行唤醒')
                self.device.wake_screen()
                self.control.wait(1)
        except Exception:
            logger.debug('唤醒设备失败，继续当前流程', exc_info=True)

    def _navigate_list(self, screen, *, allow_restart, recovering=False, launched=False):
        """One bounded navigation budget, including at most one cold restart."""
        package = self.origin or ('com.taobao.taobao4hmos' if self.device.device_type == 'hmdriver2'
                                  else 'com.taobao.taobao')
        budget = self.config['return_timeout'] + self.config['page_timeout'] * 2
        deadline = self.control.now() + budget
        back_count = resumes = 0
        restarted = self._task_restart_used
        last_action = self.control.now() if launched else self.control.now() - 10
        clicked = set()
        searched = False
        home_reset = False
        official_clicks = 0
        awaiting_activity = False
        launch_pending = launched
        while True:
            self.ensure_awake()
            self.guard(screen)
            if self._accept_list(screen):
                return screen
            if launched and screen.package != package and self.control.now() - last_action < self.config['page_timeout']:
                self.control.wait(.2)
                screen = self.read(force_ocr=bool(self.config['ocr']))
                continue
            launched = False
            # Process the last observation even if a slow OCR/RPC crossed the
            # navigation deadline. An arrived activity can still open its menu.
            if screen.package == package and (awaiting_activity or self.task_panel_entry(screen)
                                             or self.activity_landing(screen) or self.task_panel_visible(screen)):
                screen = self.enter_task_panel(screen, expected_activity=awaiting_activity)
                if self._accept_list(screen):
                    return screen
                if not allow_restart or restarted:
                    break
                self.update(message='任务菜单未打开，关闭并重开淘宝后重新读取')
                self._restart_taobao(package)
                restarted = True
                back_count = resumes = 0
                clicked.clear()
                official_clicks = 0
                awaiting_activity, launch_pending = False, True
                last_action = self.control.now()
                deadline = last_action + budget
                screen = self.read(force_ocr=bool(self.config['ocr']))
                continue
            official = self.official_activity_entry(screen) if screen.package == package else None
            if official and official_clicks < 2 and ('activity', official.bounds) not in clicked:
                self.click(official)
                official_clicks += 1
                awaiting_activity = True
                clicked.add(('activity', official.bounds))
                last_action = self.control.now()
                deadline = max(deadline, last_action + self.config['page_timeout'])
                self.control.wait(.35)
                screen = self.read(force_ocr=bool(self.config['ocr']))
                continue
            if self.control.now() >= deadline:
                break
            # Only use native homepage controls after the painted page confirms
            # its bottom navigation. Icon labels such as 首页/搜索栏 are invisible
            # to OCR; a task overlay must never expose its background as an entry.
            home_visible = self.homepage_visible(screen)
            if (home_visible or self.activity_entry(screen, homepage=home_visible)
                    or self.control.now() - last_action >= self.config['page_timeout']):
                launch_pending = False
            if (screen.package == package and
                    (self.launch_screen(screen) or (launch_pending and not home_visible
                     and self.control.now() - last_action < self.config['page_timeout']))):
                self.update(message='等待淘宝启动完成，准备点击首页领淘金币')
                self.control.wait(.35)
                screen = self.read(force_ocr=bool(self.config['ocr']))
                continue
            if home_visible:
                native = self.read()
                if native.package == package:
                    screen = Screen([*native.nodes, *screen.nodes], screen.width, screen.height,
                                    package, 'ui+ocr', native.tree)
            # Empty/splash observations get a short rendering grace period.
            ready = bool(normalize(screen.text))
            if screen.package != package:
                if resumes < 2:
                    self.device.resume_app(package)
                    resumes += 1
                    launch_pending = True
                    last_action = self.control.now()
                    self.control.wait(.8)
                elif allow_restart and not restarted:
                    self.update(message='跳转层级过深，重启淘宝并重新进入活动')
                    self._restart_taobao(package)
                    restarted, back_count = True, 0
                    clicked.clear()
                    official_clicks = 0
                    awaiting_activity, launch_pending = False, True
                    last_action = self.control.now()
                else:
                    break
            elif ready:
                action = None
                target = self.activity_entry(screen, homepage=home_visible)
                key = ('activity', target.bounds if target else None)
                if target and key not in clicked:
                    action = target
                    clicked.add(key)
                if action:
                    self.click(action)
                    awaiting_activity = True
                    last_action = self.control.now()
                elif self.control.now() - last_action < .7:
                    pass
                elif home_visible and not home_reset and screen.find('首页', exact=True):
                    self.click(screen.find('首页', exact=True))
                    home_reset = True
                    last_action = self.control.now()
                elif screen.find(r'做任务赚|累计任务奖励') and not self.tasks(screen):
                    # Wait for the panel rows instead of dismissing the loading panel.
                    if self.control.now() - last_action > self.config['page_timeout']:
                        if allow_restart and not restarted:
                            self._restart_taobao(package)
                            restarted, back_count = True, 0
                            clicked.clear()
                            official_clicks = 0
                            awaiting_activity, launch_pending = False, True
                            last_action = self.control.now()
                        else:
                            break
                elif (not searched and screen.find(r'^搜索栏$|^搜索框$')
                      and (restarted or back_count > 0 or home_reset)):
                    from modules.utils import open_taobao_search
                    open_taobao_search(self.device, self.config['search_keyword'], self.control)
                    searched = True
                    last_action = self.control.now()
                elif back_count < min(2, self.config['max_back_steps']) and not restarted:
                    self.device.go_back()
                    back_count += 1
                    last_action = self.control.now()
                elif allow_restart and not restarted:
                    self.update(message='重新打开淘宝，从首页进入活动任务')
                    self._restart_taobao(package)
                    restarted, back_count = True, 0
                    resumes = 0
                    clicked.clear()
                    official_clicks = 0
                    awaiting_activity, launch_pending = False, True
                    last_action = self.control.now()
                elif restarted and back_count < self.config['max_back_steps']:
                    # A homepage popup or a restored deep page can survive launch.
                    self.device.go_back()
                    back_count += 1
                    last_action = self.control.now()
            elif not ready and self.control.now() - last_action > 5:
                if allow_restart and not restarted:
                    self._restart_taobao(package)
                    restarted, back_count = True, 0
                    clicked.clear()
                    official_clicks = 0
                    awaiting_activity, launch_pending = False, True
                    last_action = self.control.now()
                elif self.control.now() - last_action > self.config['page_timeout']:
                    break
            self.control.wait(.35)
            screen = self.read(force_ocr=bool(self.config['ocr']))
        if allow_restart and not self._task_restart_used:
            self.update(message='导航异常，关闭并重开淘宝后重新读取任务列表')
            self._restart_taobao(package)
            screen = self.read(force_ocr=bool(self.config['ocr']))
            return self._navigate_list(screen, allow_restart=False, recovering=recovering)
        reason = ('未能自动恢复淘宝活动任务列表' if recovering else '未能自动进入淘宝活动任务列表')
        reason += '，已保存现场；请检查活动入口是否变化后重试'
        saved = self.report_failure(reason, phase='returning' if recovering else 'locating')
        raise NeedsAttention(reason, kind='navigation', evidence_saved=saved)

    def ensure_list(self, auto_open=False):
        self.ensure_awake()
        self.update('locating', message='确认当前页面并进入活动任务列表')
        package = self.origin or ('com.taobao.taobao4hmos' if self.device.device_type == 'hmdriver2'
                                  else 'com.taobao.taobao')
        if auto_open:
            foreground = self.device.current_app().get('package', '')
            # A launcher needs no screenshot/OCR. Stop the cached Taobao task
            # before opening it, so its old scrolled homepage cannot be restored.
            if isinstance(foreground, str) and ('launcher' in foreground or foreground.endswith('.home')):
                self.update(message='从桌面启动：先关闭淘宝后台，再重新进入首页')
                self._restart_taobao(package)
                screen = self.read(force_ocr=bool(self.config['ocr']))
                return self._navigate_list(screen, allow_restart=True, launched=True)
        screen = self.read(force_ocr=bool(self.config['ocr']))
        self.guard(screen)
        if self._accept_list(screen):
            return screen
        if auto_open:
            if screen.package != package:
                self.update(message='先关闭淘宝后台，再重新进入首页')
                self._restart_taobao(package)
                screen = self.read(force_ocr=bool(self.config['ocr']))
                return self._navigate_list(screen, allow_restart=True, launched=True)
            return self._navigate_list(screen, allow_restart=True)
        if self.task_panel_entry(screen) or self.activity_landing(screen) or self.task_panel_visible(screen):
            screen = self.enter_task_panel(screen)
            if self._accept_list(screen):
                return screen
        # The advanced current-page mode never launches/restarts an application.
        deadline = self.control.now() + self.config['page_timeout']
        clicked = False
        while self.control.now() < deadline:
            if not clicked:
                clicked = self.open_task_panel(screen)
            self.control.wait(.4)
            screen = self.read(force_ocr=bool(self.config['ocr']))
            self.guard(screen)
            if self._accept_list(screen):
                return screen
        reason = '未识别到任务列表，请使用自动进入活动，或先在手机打开赚能量任务列表'
        saved = self.report_failure(reason, phase='locating')
        raise NeedsAttention(reason, evidence_saved=saved)

    def recover(self, cold_start=False):
        self.update('returning', message='自动恢复淘宝活动任务列表')
        try:
            screen = self.read(force_ocr=bool(self.config['ocr']))
        except (Stopped, NeedsAttention):
            raise
        except Exception:
            if not cold_start or self._task_restart_used:
                raise
            screen = None
        if screen is not None:
            self.guard(screen)
        if cold_start and not self._task_restart_used:
            package = self.origin or ('com.taobao.taobao4hmos' if self.device.device_type == 'hmdriver2'
                                      else 'com.taobao.taobao')
            self.update(message='页面异常，关闭并重开淘宝，重新读取任务列表')
            self._restart_taobao(package)
            screen = self.read(force_ocr=bool(self.config['ocr']))
        return self._navigate_list(screen, allow_restart=True, recovering=True)

    def browse_swipe(self, screen, reverse=False):
        self.control.check()
        start_y, end_y = (.47, .77) if reverse else (.77, .47)
        self.device.swipe(int(screen.width * .5), int(screen.height * start_y),
                          int(screen.width * .5), int(screen.height * end_y), duration=.45)

    @staticmethod
    def search_results_ready(screen):
        if screen.find(r'^历史搜索$|^搜索发现$|^猜你想搜$|^搜索推荐$'):
            return False
        filters = sum(bool(screen.find(pattern)) for pattern in
                      (r'^综合', r'^销量', r'^筛选', r'^店铺', r'^价格'))
        if filters >= 2 and len(screen.nodes) >= 8:
            return True
        # Double 11's embedded search results use an orange browse countdown
        # and a product grid instead of the normal sort/filter toolbar.
        banner = screen.find(r'浏览\d+[秒sS](?:可领|可得|得)|浏览完成.*(?:奖励|能量)')
        search = next((n for n in screen.nodes if normalize(n.text) == '搜索'
                       and n.center[1] < screen.height * .25), None)
        products = [n for n in screen.nodes if search and n.center[1] > search.bounds[3]
                    and re.search(r'[¥￥]\s*\d|金币热卖价|金币已抵', n.text)]
        return bool(banner and search and len(products) >= 2 and len(screen.nodes) >= 8)

    @staticmethod
    def search_controls(tree):
        edits, buttons = [], []
        def walk(item):
            if isinstance(item, list):
                for child in item:
                    walk(child)
            elif isinstance(item, dict):
                attrs = item.get('attributes', item)
                kind = attrs.get('type', attrs.get('class', ''))
                if 'EditText' in kind or 'TextInput' in kind:
                    edits.append(attrs)
                elif (normalize(attrs.get('text') or attrs.get('description') or '') == '搜索'
                      and ('Button' in kind or 'searchbtn' in attrs.get('id', '').lower())):
                    buttons.append(TextNode('搜索', bounds_of(attrs.get('bounds', attrs.get('rect')))))
                for key in ('children', 'hierarchy'):
                    if isinstance(item.get(key), (list, dict)):
                        walk(item[key])
        walk(tree)
        return edits, buttons

    @staticmethod
    def _submit_buttons(screen, buttons=()):
        # Input hints may also say 搜索. Only the narrow upper-right action counts.
        return [n for n in [*buttons, *screen.nodes] if normalize(n.text) == '搜索'
                and n.center[0] > screen.width * .7 and 0 < n.center[1] < screen.height * .25
                and 0 < n.bounds[2] - n.bounds[0] < screen.width * .3]

    def handle_search(self, screen):
        """Fill the native field, relocate Submit, retry then fall back to Enter."""
        if self.search_results_ready(screen):
            return True
        tree = screen.tree if screen.tree is not None else self.device.dump_hierarchy()
        edits, _ = self.search_controls(tree)
        edit = next((e for e in edits if e.get('focused') in (True, 'true')),
                    edits[0] if len(edits) == 1 else None)
        if not edit:
            logger.warning('没有可确认的输入框，不开始浏览计时')
            self.report_failure('搜索页没有可确认的输入框', phase='searching')
            return False
        query = self.config.get('task_search_keyword', '手机壳')
        if normalize(edit.get('text', '')) != normalize(query):
            if self.device.device_type == 'hmdriver2':
                self.click(TextNode('', bounds_of(edit.get('bounds', edit.get('rect')))))
            self.control.check()
            # set_text replaces the field without installing/switching the IME.
            self.device.input_text(query)
            self.control.wait(.2)
        self.update('searching', message='提交搜索并确认商品结果页')
        deadline = self.control.now() + self.config['page_timeout']
        submitted, next_submit = 0, self.control.now()
        result = screen
        while self.control.now() < deadline:
            self.control.check()
            if self.control.now() >= next_submit and submitted < 3:
                # Always relocate after input or a failed click; the reward header
                # can move the button and a landing overlay can replace the page.
                fresh_tree = self.device.dump_hierarchy()
                _, native_buttons = self.search_controls(fresh_tree)
                native = [n for n in self._submit_buttons(result, native_buttons)
                          if n in native_buttons]
                if not native:
                    result = self.read(force_ocr=bool(self.config['ocr']))
                    self.guard(result)
                    if self.search_results_ready(result):
                        return True
                buttons = native or self._submit_buttons(result)
                if submitted < 2 and buttons:
                    # Prefer the native position, then the rightmost visible action.
                    button = max(buttons, key=lambda n: n.center[0])
                    self.click(button)
                else:
                    # Hardware Enter does not invoke the driver's IME helper.
                    self.device.press('enter')
                    submitted = 2
                submitted += 1
                next_submit = self.control.now() + max(1, min(2, self.config['page_timeout'] / 4))
            self.control.wait(.35)
            result = self.read(force_ocr=bool(self.config['ocr']))
            self.guard(result)
            self.check_destination(result)
            if self.search_results_ready(result):
                logger.info('已确认进入搜索结果页，开始浏览计时')
                return True
            if is_task_list(result.nodes):
                break
        self.report_failure('搜索提交后未识别到商品结果页', phase='searching')
        logger.warning('搜索按钮重试及回车后仍未确认结果页，结束本次尝试')
        return False

    def execute(self, task, before):
        self._active_task = task
        self.update('opening', current_task=task.title, message='打开任务页面', remaining=0)
        logger.info('执行 %s (%d/%d)，类型 %s', task.title, task.current, task.total, task.kind)
        self.control.check()
        left, top, right, bottom = task.bounds
        self.device.click((left + right) // 2, (top + bottom) // 2)
        self.control.wait(.6)
        deadline = self.control.now() + self.config['page_timeout']
        screen, left = before, False
        searched = False
        while self.control.now() < deadline:
            screen = self.read(force_ocr=bool(self.config['ocr']))
            # Always make the first real jump attempt. Installation evidence is
            # read from this device only, never from a bundled skip list.
            self.guard(screen)
            self.check_destination(screen)
            if screen.package == self.origin and is_task_list(screen.nodes):
                after = next((x for x in self.tasks(screen) if x.key == task.key), None)
                if task.kind == 'claim' and after and (after.action != task.action or after.current > task.current):
                    return screen, True, False
                if task.key.startswith('milestone-') and after is None and self.milestone_received(task, screen):
                    return screen, True, False
                self.control.wait(.6)
                continue
            # Scoped external-app launch confirmation; never accept privacy or login grants.
            if screen.find(r'打开.{0,10}应用|是否.{0,12}打开|即将.{0,10}跳转|允许.{0,12}打开|尝试打开'):
                button = screen.find(r'^(打开|立即打开|继续打开|允许打开|去支付宝|去看看|允许)$')
                if button:
                    if not self.config['allow_cross_app']:
                        break
                    self.click(button)
                    self.control.wait(.8)
                    continue
            if not normalize(screen.text) or len(screen.nodes) < 2:
                self.control.wait(.7)
                continue
            left = True
            break
        if not left:
            logger.warning('任务没有成功离开列表：%s', task.title)
            reason = self.availability_reason(task, check_installed=bool(task.target_packages))
            if reason:
                raise TaskUnavailable(reason)
            return self.recover(), False, False
        cross = screen.package != self.origin
        if cross:
            if not self.config['allow_cross_app']:
                return self.recover(), False, False
            self.stats.cross_app += 1
            logger.info('跨应用浏览：%s', screen.package)
        # Captions rotate between browse/search promotions. The destination is
        # authoritative when a browse caption opens the activity search form.
        search_landing = (task.kind == 'browse' and screen.package == self.origin
                          and screen.find(r'^历史搜索$|^搜索发现$|^搜索有福利$|^搜索后浏览立得奖励$'))
        if task.kind == 'search' or search_landing:
            searched = self.handle_search(screen)
            if not searched:
                return self.recover(), False, cross
            screen = self.read()
        seconds = task.seconds or (self.config['visit_seconds'] if task.kind == 'visit'
                                    else self.config['default_browse_seconds'])
        if task.kind in ('browse', 'search'):
            seconds += self.config['browse_extra_seconds']
        seconds += self.config['settle_seconds']
        browse_started = self.control.now()
        hard_deadline = browse_started + self.config['max_task_seconds']
        browse_deadline = browse_started + min(seconds, self.config['max_task_seconds'])
        remaining = browse_deadline - self.control.now()
        explicit_success = False
        swipe_count = 0
        next_observation = self.control.now() + 1
        while remaining > 0:
            self.control.check()
            self.check_destination(screen)
            self.guard(screen)
            if screen.package == self.origin and is_task_list(screen.nodes):
                break
            if screen.package != self.origin:
                if not cross:
                    self.stats.cross_app += 1
                    cross = True
                if not self.config['allow_cross_app']:
                    break
            if screen.find(r'任务已完成|浏览已完成|浏览完成|完成浏览|奖励已到账|恭喜完成|任务完成啦'):
                explicit_success = True
                logger.info('检测到任务完成提示')
                break
            # Visible remaining countdown may extend, but never exceed the task cap.
            countdown = re.search(r'(?:还需|再逛|再浏览|剩余|再看)[^\d]{0,3}(\d{1,2})\s*秒', screen.text)
            if countdown:
                browse_deadline = max(browse_deadline, min(self.control.now() + int(countdown.group(1)) + 1,
                                                           hard_deadline))
            remaining = browse_deadline - self.control.now()
            self.update('browsing', message='跨应用浏览中' if cross else '浏览中', remaining=max(0, int(remaining + .9)))
            # Sustained scroll gestures keep the activity's browse counter running.
            # Alternate pairs of gestures so finite pages do not stay at the bottom.
            scrolling = self.config['scroll_browse'] and not cross and task.kind in ('browse', 'search')
            if scrolling and remaining > 0:
                self.browse_swipe(screen, reverse=swipe_count % 4 >= 2)
                swipe_count += 1
            interval = max(0, min(.35 if scrolling else 1, browse_deadline - self.control.now()))
            self.control.wait(interval)
            # Pause time must not count as browse time; wait() itself excludes pauses.
            remaining = browse_deadline - self.control.now()
            if remaining > 0 and self.control.now() >= next_observation:
                screen = self.read()
                next_observation = self.control.now() + 1
                remaining = browse_deadline - self.control.now()
        return self.recover(), explicit_success, cross

    @staticmethod
    def list_progress(screen):
        text = normalize(screen.text)
        found = re.search(r'当前进度(\d+)\s*/\s*\d+', text)
        return int(found.group(1)) if found else None

    @staticmethod
    def milestone_received(task, screen):
        header = screen.find('累计任务奖励', exact=True)
        if not header:
            return False
        center_x = (task.bounds[0] + task.bounds[2]) / 2
        return any('已领取' in normalize(n.text)
                   and abs(n.center[0] - center_x) < screen.width * .13
                   and header.bounds[1] - screen.height * .02 < n.center[1] < header.bounds[3] + screen.height * .07
                   and n.bounds[2] - n.bounds[0] < screen.width * .5 for n in screen.nodes)

    def verify(self, task, before, after, explicit=False):
        after_task = next((x for x in self.tasks(after) if x.key == task.key), None)
        if after_task and (after_task.current > task.current or after_task.kind == 'done'
                           or (task.kind != 'claim' and after_task.action in CLAIM_ACTIONS)):
            return True
        if task.kind == 'claim' and after_task and after_task.action != task.action:
            return True
        if task.key.startswith('milestone-'):
            return after_task is None and self.milestone_received(task, after)
        old, new = self.list_progress(before), self.list_progress(after)
        if old is not None and new is not None and new > old:
            return True
        # At the final search repetition Taobao removes the row entirely. The
        # cumulative counter can already be capped at 50/50, so require both
        # visible neighbours and their row-gap contraction instead of assuming
        # that any off-screen/missing row completed.
        if (after_task is None and task.kind in ('browse', 'search', 'visit')
                and task.current == task.total - 1
                and (task.total > 1 or after.source == 'ocr')):
            rows = sorted((t for t in self.tasks(before) if not t.key.startswith('milestone-')),
                          key=lambda t: t.bounds[1])
            index = next((i for i, t in enumerate(rows) if t.key == task.key), -1)
            if 0 < index < len(rows) - 1:
                upper, lower = rows[index - 1], rows[index + 1]
                found = {t.key: t for t in self.tasks(after)}
                if upper.key in found and lower.key in found:
                    upper_after, lower_after = found[upper.key], found[lower.key]
                    gap_before = lower.bounds[1] - upper.bounds[1]
                    gap_after = lower_after.bounds[1] - upper_after.bounds[1]
                    if (abs(upper_after.bounds[1] - upper.bounds[1]) < after.height * .025
                            and 0 < gap_after < gap_before * .75):
                        return True
            elif index == 0 and len(rows) >= 3 and after.source == 'ocr':
                # The first one-off slot also disappears after a successful
                # jump. Require the fixed reward header and TWO following rows
                # moving up together, confirmed from the rendered screenshot.
                header_before = before.find('累计任务奖励', exact=True)
                header_after = after.find('累计任务奖励', exact=True)
                found = {t.key: t for t in self.tasks(after)}
                following = [t for t in rows[1:] if t.key in found][:2]
                if (header_before and header_after
                        and abs(header_before.center[1] - header_after.center[1]) < after.height * .025
                        and len(following) == 2 and following[0].key == rows[1].key):
                    shifts = [t.bounds[1] - found[t.key].bounds[1] for t in following]
                    if (min(shifts) > after.height * .04 and abs(shifts[0] - shifts[1]) < after.height * .025
                            and abs(found[following[0].key].bounds[1] - task.bounds[1]) < after.height * .04):
                        return True
        return explicit and after_task is None

    def scan(self, auto_open=False):
        screen = self.ensure_list(auto_open)
        self.remember(self.tasks(screen))
        self.update('ready', message='已识别当前屏幕任务，可以开始执行')
        return list(self.records.values())

    def run(self, auto_open=False):
        self.started = time.monotonic()
        try:
            screen = self.ensure_list(auto_open)
        except (Stopped, NeedsAttention):
            raise
        except Exception:
            if not auto_open or self._task_restart_used:
                raise
            self.report_failure('初始页面读取异常', phase='locating')
            screen = self.recover(cold_start=True)
        seen_pages = set()
        scrolls, total_attempts = 0, 0
        while total_attempts < self.config['max_tasks']:
            self.control.check()
            self.guard(screen)
            tasks = self.tasks(screen)
            self.remember(tasks)
            candidates = []
            for task in tasks:
                failure_key = (task.key, task.current)
                if task.kind in ('done', 'manual') or self.records[task.key]['status'] == 'disabled':
                    continue
                if task.kind not in self.config['enabled_task_kinds']:
                    self.records[task.key].update(status='disabled', reason='本轮未选择该任务类型；调整设置后可再次执行')
                    continue
                if task.kind == 'claim' and not self.config['claim_rewards']:
                    continue
                if not self.config['allow_cross_app'] and task.target_app:
                    self.records[task.key].update(status='disabled', reason='跨应用任务已关闭')
                    continue
                if self.cross_app_blocked(task):
                    continue
                if self.failures[failure_key] < self.config['max_attempts']:
                    candidates.append(task)
            if candidates:
                task = min(candidates, key=lambda t: (self.failures[(t.key, t.current)],
                                                      {'claim': 0, 'browse': 1, 'visit': 2, 'search': 3}.get(t.kind, 4), t.bounds[1]))
                total_attempts += 1
                self.stats.attempts += 1
                self.records[task.key]['status'] = 'running'
                self.update(tasks=list(self.records.values()))
                before = screen
                disabled_reason = ''
                self._task_restart_used = False
                try:
                    after, explicit, cross = self.execute(task, before)
                    success = self.verify(task, before, after, explicit)
                    if not success:
                        # A single short refresh accommodates server-side settlement.
                        self.control.wait(1)
                        # Themis can retain a completed/removed row in its UI
                        # tree. Recheck the rendered page before retrying it.
                        after = self.read(force_ocr=bool(self.config['ocr'] and after.source == 'ui'))
                        if not is_task_list(after.nodes):
                            after = self.recover()
                        success = self.verify(task, before, after, explicit)
                    screen = after
                    if task.kind == 'claim' and success:
                        self.control.wait(.8)
                        screen = self.read()
                        if not is_task_list(screen.nodes):
                            screen = self.recover()
                except TaskUnavailable as exc:
                    self.report_failure(str(exc), task, phase='unavailable')
                    disabled_reason = self.availability_reason(task, check_installed=bool(task.target_packages)) or str(exc)
                    self.unavailable_tasks[self.unavailable_key(task)] = disabled_reason
                    screen = self.recover()
                    success = False
                except Stopped:
                    raise
                except NeedsAttention as exc:
                    if exc.kind != 'navigation' or self._task_restart_used:
                        raise
                    screen = self.recover(cold_start=True)
                    success = self.verify(task, before, screen)
                except Exception:
                    logger.exception('任务执行异常：%s', task.title)
                    self.report_failure('任务执行异常', task)
                    screen = self.recover(cold_start=True)
                    # Reopening may reveal already-settled server progress.
                    success = self.verify(task, before, screen)
                if not success and not disabled_reason and task.target_packages:
                    # Some deeplinks silently stay on a landing page. Only after
                    # that real attempt fails verification do we confirm absence.
                    disabled_reason = self.availability_reason(task, check_installed=True)
                self.remember(self.tasks(screen))
                if disabled_reason:
                    self.records[task.key].update(status='disabled', reason=disabled_reason)
                    logger.info('已跳过 %s：%s', task.title, disabled_reason)
                elif success:
                    if task.kind == 'claim':
                        self.stats.claims += 1
                    else:
                        self.stats.completed += 1
                    updated = next((t for t in self.tasks(screen) if t.key == task.key), None)
                    can_continue = updated and updated.current < updated.total and updated.kind not in ('done', 'claim')
                    self.records[task.key].update(status='pending' if can_continue else 'done',
                                                  reason='已核验本次进度，可继续执行' if can_continue else '已核验任务进度或奖励状态')
                    if updated is None and task.kind == 'claim':
                        self.records[task.key].update(current=task.total, reason='累计任务奖励已领取，已核验奖励状态')
                    elif updated is None and task.current == task.total - 1:
                        self.records[task.key].update(current=task.total, reason='任务完成后已从列表移除，已核验相邻任务位置')
                    logger.info('核验成功：%s', task.title)
                else:
                    self.report_failure('任务进度未增加', task, phase='verifying')
                    self.failures[(task.key, task.current)] += 1
                    exhausted = self.failures[(task.key, task.current)] >= self.config['max_attempts']
                    if exhausted:
                        self.stats.failed += 1
                    self.records[task.key].update(status='failed' if exhausted else 'pending',
                                                  reason='任务进度未增加' + ('，已达尝试上限' if exhausted else '，先执行其他任务'))
                    logger.warning('未核验成功：%s，当前进度 %d，尝试 %d/%d', task.title, task.current,
                                   self.failures[(task.key, task.current)], self.config['max_attempts'])
                self.update(tasks=list(self.records.values()), remaining=0)
                # Progress changed: the same viewport may have newly available tasks.
                seen_pages.clear()
                continue
            signature = tuple((t.key, t.current, t.action) for t in tasks)
            if signature in seen_pages or scrolls >= self.config['max_scroll_pages']:
                break
            seen_pages.add(signature)
            self.update('scanning', message='检查列表下方的任务', current_task='', remaining=0)
            self.control.check()
            self._task_restart_used = False
            try:
                self.device.swipe(int(screen.width * .5), int(screen.height * .88),
                                  int(screen.width * .5), int(screen.height * .43), duration=.55)
                scrolls += 1
                self.control.wait(.7)
                screen = self.read()
                if not is_task_list(screen.nodes):
                    screen = self.recover()
            except (Stopped, NeedsAttention):
                raise
            except Exception:
                logger.exception('任务列表读取异常')
                self.report_failure('任务列表读取异常', phase='scanning')
                screen = self.recover(cold_start=True)
        message = ('达到本轮任务次数上限，可再次开始继续' if total_attempts >= self.config['max_tasks']
                   else '本轮结束，可执行任务已处理；请查看待处理和未通过核验的任务')
        self.update('finished', message=message, current_task='', remaining=0,
                    tasks=list(self.records.values()))
        return asdict(self.stats)
