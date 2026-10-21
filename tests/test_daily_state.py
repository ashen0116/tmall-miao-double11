import io
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile
from urllib.error import HTTPError
from urllib.request import urlopen

from PIL import Image

from modules.control_panel import Controller, make_handler
from modules.campaign import NeedsAttention, load_config
from modules.daily_state import DailyState


class DailyStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def store(self, serial='test-phone-a', day='2026-10-19'):
        return DailyState(self.root, serial, 'uiautomator2', day)

    def test_run_high_watermarks_survive_reload_and_scans_do_not_inflate_totals(self):
        store = self.store()
        tasks = [dict(key='manual-one', title='邀请好友', status='manual')]
        store.progress('first-run', dict(completed=1, attempts=1, elapsed=15), tasks,
                       {'app:未安装示例': '未安装示例应用，已跳过'})
        store.progress('first-run', dict(completed=1, attempts=1, elapsed=15), tasks)
        store.progress('first-run', dict(completed=2, attempts=2, elapsed=32), tasks)
        reloaded = self.store()
        reloaded.progress('first-run', dict(completed=2, attempts=2, elapsed=32), tasks)
        reloaded.progress('scan', dict(completed=0, skipped=1, elapsed=99), tasks, count_stats=False)
        reloaded.progress('second-run', dict(completed=1, claims=1, attempts=2, elapsed=8), tasks)
        stats = reloaded.snapshot()['stats']
        self.assertEqual((stats['completed'], stats['claims'], stats['attempts'], stats['elapsed'], stats['skipped']),
                         (3, 1, 4, 40, 1))
        self.assertIn('app:未安装示例', reloaded.unavailable_tasks())
        self.assertEqual(reloaded.snapshot()['unavailable_count'], 1)
        self.assertNotIn('test-phone-a', reloaded.stats_path.read_text(encoding='utf-8'))

    def test_new_device_and_day_are_isolated_and_no_raw_identity_in_paths(self):
        today = self.store()
        today.progress('run', dict(completed=2), unavailable_tasks={'app:示例': '未安装'})
        other_phone = self.store(serial='test-phone-b')
        tomorrow = self.store(day='2026-10-20')
        for fresh in (other_phone, tomorrow):
            self.assertEqual(fresh.snapshot()['stats']['completed'], 0)
            self.assertEqual(fresh.unavailable_tasks(), {})
            self.assertNotIn('test-phone', str(fresh.folder))
        self.assertEqual(self.store().snapshot()['stats']['completed'], 2)

    def test_cross_midnight_run_counts_only_new_day_increases(self):
        tomorrow = self.store(day='2026-10-20')
        tomorrow.progress('overnight-run', dict(completed=4, attempts=5, elapsed=90),
                          baseline=dict(completed=3, attempts=4, elapsed=80))
        tomorrow.progress('overnight-run', dict(completed=5, attempts=6, elapsed=110))
        stats = tomorrow.snapshot()['stats']
        self.assertEqual((stats['completed'], stats['attempts'], stats['elapsed']), (2, 2, 30))

    def test_temporary_task_filters_are_not_persisted_as_daily_manual_work(self):
        store = self.store()
        store.progress('run', {}, tasks=[
            dict(key='filtered', status='disabled', reason='本轮未选择该任务类型；调整设置后可再次执行'),
            dict(key='cross-app-off', status='disabled', reason='跨应用任务已关闭'),
            dict(key='not-installed', status='disabled', reason='未安装示例应用'),
        ])
        self.assertEqual(store.snapshot()['stats']['skipped'], 1)

    def test_failures_keep_png_and_json_and_export_only_selected_day_device(self):
        store = self.store()
        store.log('INFO', '连接设备 test-phone-a')
        event = dict(reason='需要安全验证', task=dict(title='浏览任务'), phase='browse')
        evidence = store.failure(event, Image.new('RGB', (60, 100), 'white'))
        self.assertEqual(store.failure(event, Image.new('RGB', (60, 100))), evidence)
        self.assertEqual(store.snapshot()['failures_count'], 1)
        self.assertTrue(Path(evidence['screenshot']).is_file())
        metadata = json.loads(Path(evidence['metadata']).read_text(encoding='utf-8'))
        self.assertEqual(metadata['reason'], '需要安全验证')
        self.store('another-phone').failure(dict(reason='其他设备不能混入'))
        exported = store.diagnostics(dict(device=dict(serial='test-phone-a'), config={'secret': 'example'}))
        with zipfile.ZipFile(io.BytesIO(exported)) as archive:
            self.assertEqual(len([name for name in archive.namelist() if name.endswith('.png')]), 1)
            report = json.loads(archive.read('report.json'))
            self.assertEqual(report['device']['serial'], '[设备标识已隐藏]')
            self.assertNotIn('config', report)
            self.assertNotIn('test-phone-a', archive.read('events.jsonl').decode())
            self.assertIn('个人信息', archive.read('READ-ME.txt').decode())

    def test_failure_callback_kind_is_saved_without_colliding_with_journal_event_type(self):
        store = self.store()
        event = dict(reason='自动化调用异常', phase='error', kind='exception', time='2026-10-19T12:00:00+08:00')
        result = store.failure(event, Image.new('RGB', (20, 40)))
        self.assertTrue(Path(result['metadata']).exists())
        journal = [json.loads(line) for line in store.events_path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(journal[-1]['kind'], 'failure')
        self.assertEqual(journal[-1]['detail_kind'], 'exception')
        self.assertEqual(self.store().snapshot()['failures_count'], 1)

    def test_screenshot_failure_still_keeps_reason_and_corrupt_state_is_preserved(self):
        store = self.store()
        item = store.failure(dict(reason='连接中断', phase='restore', capture_error='device offline'))
        self.assertEqual(item['screenshot'], '')
        self.assertEqual(json.loads(Path(item['metadata']).read_text(encoding='utf-8'))['capture_error'], 'device offline')
        store.stats_path.write_text('{broken', encoding='utf-8')
        recovered = self.store()
        self.assertTrue(recovered.recovery_warning)
        self.assertEqual(len(list(recovered.folder.glob('state.corrupt-*.json'))), 1)

    def test_missing_resolved_identity_is_rejected(self):
        for value in (None, '', '自动选择', ' 自动选择 '):
            with self.assertRaises(ValueError):
                DailyState(self.root, value, 'uiautomator2')

    def test_malformed_structured_state_is_recovered_before_mutating_defaults(self):
        store = self.store()
        damaged = json.loads(store.stats_path.read_text(encoding='utf-8'))
        damaged.update(stats={'completed': 99}, skipped_task_keys=None, failures_count='bad')
        store.stats_path.write_text(json.dumps(damaged), encoding='utf-8')
        recovered = self.store()
        self.assertTrue(recovered.recovery_warning)
        self.assertEqual(recovered.snapshot()['stats']['completed'], 0)
        recovered.progress('new-run', dict(completed=1))
        self.assertEqual(recovered.snapshot()['stats']['completed'], 1)


class ControllerDailyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch('modules.control_panel.data_dir', return_value=Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.controller = Controller()
        self.controller.device = Mock(serial='resolved-test-phone', device_type='uiautomator2')
        self.controller.device.io_lock = threading.RLock()
        self.controller.device.screenshot.return_value = Image.new('RGB', (50, 100), 'white')

    def test_restart_same_device_loads_totals_and_availability_without_scanning_reset(self):
        c = self.controller
        with patch('modules.control_panel.local_day', return_value='2026-10-19'):
            c._ensure_daily()
            c.unavailable_tasks['app:示例'] = '未安装示例，已跳过'
            c._runner_event('run', True, stats=dict(completed=2, elapsed=20), phase='done')
            new = Controller()
            new.device = c.device
            new._ensure_daily()
            self.assertEqual(new.snapshot()['daily']['stats']['completed'], 2)
            self.assertIn('app:示例', new.unavailable_tasks)
            new._runner_event('scan', False, stats=dict(completed=0, elapsed=5), tasks=[])
            self.assertEqual(new.snapshot()['daily']['stats']['completed'], 2)

    def test_usb_and_wireless_transport_share_daily_hardware_identity(self):
        c = self.controller
        c.device.identity_serial = 'test-hardware-identity'
        c._runner_event('usb-run', True, stats=dict(completed=2))
        device_key = c.daily.device_key
        c.device = Mock(serial='192.0.2.10:5555', identity_serial='test-hardware-identity',
                        device_type='uiautomator2')
        self.assertFalse(c._ensure_daily())
        self.assertEqual(c.daily.device_key, device_key)
        self.assertEqual(c.snapshot()['daily']['stats']['completed'], 2)

    def test_midnight_preserves_runner_cache_reference_and_resets_its_contents(self):
        c = self.controller
        with patch('modules.control_panel.local_day', return_value='2026-10-19'):
            c._ensure_daily()
            cache = c.unavailable_tasks
            cache['app:示例'] = '未安装示例，已跳过'
            c._runner_event('overnight', True, stats=dict(completed=2, elapsed=20))
        with patch('modules.control_panel.local_day', return_value='2026-10-20'):
            c._runner_event('overnight', True, stats=dict(completed=3, elapsed=24))
            self.assertIs(cache, c.unavailable_tasks)
            self.assertFalse(cache)
            self.assertEqual(c.snapshot()['daily']['stats']['completed'], 1)
            self.assertEqual(c.snapshot()['daily']['stats']['elapsed'], 4)

    def test_diagnostics_switching_day_before_runner_event_does_not_import_yesterday_totals(self):
        c = self.controller
        with patch('modules.control_panel.local_day', return_value='2026-10-19'):
            c._runner_event('overnight', True, stats=dict(completed=8, attempts=9, elapsed=120))
        with patch('modules.control_panel.local_day', return_value='2026-10-20'):
            # Export/stop/pause may switch the store before the runner emits.
            c.diagnostics()
            self.assertEqual(c.snapshot()['daily']['stats']['completed'], 0)
            c._runner_event('overnight', True, stats=dict(completed=9, attempts=10, elapsed=125))
            self.assertEqual(c.snapshot()['daily']['stats']['completed'], 1)
            self.assertEqual(c.snapshot()['daily']['stats']['attempts'], 1)
            self.assertEqual(c.snapshot()['daily']['stats']['elapsed'], 5)

    def test_connection_progress_is_visible_and_failed_reconnect_detaches_previous_phone(self):
        c = self.controller
        c._ensure_daily()
        c.on_image(Image.new('RGB', (20, 40)))
        c.log('INFO', '旧手机 resolved-test-phone')
        stages = []
        def failed_connect(kind, serial, on_status):
            on_status('正在等待手机调试授权')
            stages.append(c.snapshot())
            raise ConnectionError('等待授权超时')
        with patch('modules.device_adapter.DeviceAdapter', side_effect=failed_connect):
            with self.assertLogs('modules.control_panel', level='ERROR'):
                c._work('connect', {}, load_config())
        self.assertEqual(stages[0]['status'], 'connecting')
        self.assertEqual(stages[0]['message'], '正在等待手机调试授权')
        self.assertFalse(stages[0]['connected'])
        final = c.snapshot()
        self.assertEqual(final['status'], 'error')
        self.assertEqual(final['phase'], 'connect_failed')
        self.assertIn('等待授权超时', final['message'])
        self.assertFalse(final['connected'])
        self.assertIsNone(c.device)
        self.assertIsNone(c.daily)
        self.assertIsNone(c.image)
        self.assertFalse(any('旧手机' in line['message'] for line in final['logs']))
        with self.assertRaisesRegex(ValueError, '请先连接设备'):
            c.diagnostics()

    def test_diagnostics_http_without_device_returns_actionable_400_and_connected_export_is_zip(self):
        c = self.controller
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(c, 'test-token'))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/api/diagnostics'
            c.device = None
            with self.assertRaises(HTTPError) as denied:
                urlopen(url, timeout=2)
            self.assertEqual(denied.exception.code, 400)
            self.assertIn('请先连接设备', json.loads(denied.exception.read())['error'])
            c.device = Mock(serial='test-http-phone', device_type='uiautomator2')
            c._ensure_daily()
            with urlopen(url, timeout=2) as response:
                self.assertEqual(response.headers['Content-Type'], 'application/zip')
                with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                    self.assertIn('state.json', archive.namelist())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_failure_and_preview_callbacks_do_not_require_panel_lock_for_device_calls(self):
        c = self.controller
        def screenshot():
            self.assertFalse(c.lock._is_owned())
            return Image.new('RGB', (50, 100), 'white')
        c.device.screenshot.side_effect = screenshot
        c.on_failure(dict(reason='测试失败', phase='verify'))
        self.assertEqual(c.snapshot()['daily']['failures_count'], 1)
        c.on_image(Image.new('RGB', (50, 100)))
        c.on_image(Image.new('RGB', (50, 100)))
        self.assertEqual(c.snapshot()['image_sequence'], 2)
        self.assertTrue(c.snapshot()['image_updated_at'])

    def test_reported_guard_followed_by_exception_keeps_one_rich_evidence_record(self):
        c = self.controller
        def fail_after_callback(auto_open):
            c.on_failure(dict(reason='检测到安全验证，需要本人处理', phase='browse',
                              task={'title': '浏览任务'}, kind='verification'))
            raise NeedsAttention('检测到安全验证，需要本人处理')
        runner = Mock()
        runner.run.side_effect = fail_after_callback
        with patch('modules.control_panel.CampaignRunner', return_value=runner):
            c._work('run', {}, load_config())
        result = c.snapshot()
        self.assertEqual(result['status'], 'needs_attention')
        self.assertEqual(result['daily']['failures_count'], 1)
        self.assertEqual(result['last_failure']['task']['title'], '浏览任务')
        self.assertEqual(result['last_failure']['phase'], 'browse')

    def test_preview_skips_busy_device_lock(self):
        c = self.controller
        held = threading.Event()
        release = threading.Event()
        def hold_device():
            with c.device.io_lock:
                held.set()
                release.wait(3)
        holder = threading.Thread(target=hold_device)
        holder.start()
        self.assertTrue(held.wait(1))
        c._start_preview()
        try:
            self.assertFalse(c.device.screenshot.called)
            self.assertFalse(c._preview_stop.wait(.35))
            self.assertFalse(c.device.screenshot.called)
        finally:
            c._preview_stop.set()
            release.set()
            holder.join(1)
            c._preview_thread.join(1)

    def test_security_exception_notifies_desktop_without_duplicate_capture_or_resume(self):
        c = self.controller
        def stop_for_security(auto_open):
            c.on_failure(reason='请本人验证', phase='security', task={'title': '浏览任务'})
            raise NeedsAttention('请本人验证', kind='security', evidence_saved=True)
        runner = Mock()
        runner.run.side_effect = stop_for_security
        with patch('modules.control_panel.CampaignRunner', return_value=runner), \
                patch('modules.control_panel.notify_security_attention') as notify:
            c._work('run', {}, load_config())
        notify.assert_called_once_with()
        runner.run.assert_called_once()
        self.assertEqual(c.snapshot()['status'], 'needs_attention')
        self.assertEqual(c.snapshot()['daily']['failures_count'], 1)
        self.assertEqual(c.snapshot()['last_failure']['phase'], 'security')

    def test_navigation_attention_has_no_security_popup(self):
        c = self.controller
        runner = Mock()
        runner.run.side_effect = NeedsAttention('未能进入任务菜单')
        with patch('modules.control_panel.CampaignRunner', return_value=runner), \
                patch('modules.control_panel.notify_security_attention') as notify:
            c._work('run', {}, load_config())
        notify.assert_not_called()
        self.assertEqual(c.snapshot()['daily']['failures_count'], 1)


if __name__ == '__main__':
    unittest.main()
