"""Double 11 assistant: local web panel by default, or a scriptable CLI."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

if getattr(sys, 'frozen', False):
    tools_dir = Path(sys._MEIPASS) / 'tools'
    if tools_dir.is_dir():
        os.environ['PATH'] = str(tools_dir) + os.pathsep + os.environ.get('PATH', '')
        adb = tools_dir / 'adb.exe'
        if adb.exists():
            os.environ['ADBUTILS_ADB_PATH'] = str(adb)


def main():
    parser = argparse.ArgumentParser(description='淘宝双11任务助手：本地网页控制台 / 命令行')
    parser.add_argument('--cli', action='store_true', help='使用命令行执行任务')
    parser.add_argument('--port', type=int, default=8765, help='本机控制台端口，默认 8765')
    parser.add_argument('--no-browser', action='store_true', help='不自动打开浏览器')
    parser.add_argument('--self-test', action='store_true', help='检查打包资源、ADB 和离线 OCR，不操作手机')
    parser.add_argument('--output', help='自检 JSON 输出文件')
    parser.add_argument('--port-file', help='将服务 URL 写入指定文件，供打包验证使用')
    parser.add_argument('--device', choices=['android', 'harmony'], default='android')
    parser.add_argument('--serial', help='USB 序列号或已连接的无线调试地址')
    parser.add_argument('--auto-open', action='store_true', help='尝试从淘宝首页自动进入活动')
    parser.add_argument('--scan', action='store_true', help='CLI 仅识别任务，不执行')
    parser.add_argument('--config', help='覆盖默认配置的 JSON 文件')
    parser.add_argument('--max-tasks', type=int, help='本轮最大尝试次数')
    parser.add_argument('--no-ocr', action='store_true', help='关闭本地文字识别')
    parser.add_argument('--no-cross-app', action='store_true', help='跳过跨应用任务')
    parser.add_argument('--cross-apps', help='只允许这些应用的跳转任务，逗号分隔，例如 支付宝,闲鱼')
    parser.add_argument('--log', dest='loglevel', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    args = parser.parse_args()
    from modules.runtime import data_dir
    handlers = [RotatingFileHandler(data_dir() / 'application.log', maxBytes=2_000_000,
                                   backupCount=2, encoding='utf-8')]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=getattr(logging, args.loglevel), handlers=handlers,
                        format='%(asctime)s %(levelname)-7s %(message)s', datefmt='%H:%M:%S')
    if args.self_test:
        from modules.diagnostics import self_test
        result = self_test(args.output or str(data_dir() / 'self-test.json'))
        if sys.stdout is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result['ok'] else 1)
    from modules.campaign import CampaignRunner, NeedsAttention, Stopped, load_config, validate_config
    config = load_config(args.config)
    if args.max_tasks is not None:
        config['max_tasks'] = args.max_tasks
    if args.no_ocr:
        config['ocr'] = False
    if args.no_cross_app:
        config['allow_cross_app'] = False
    if args.cross_apps:
        config['allowed_cross_apps'] = [part.strip() for part in args.cross_apps.split(',') if part.strip()]
    validate_config(config)
    if not args.cli:
        from modules.control_panel import serve
        try:
            serve(args.port, not args.no_browser, config, port_file=args.port_file)
        except OSError as exc:
            parser.exit(1, f'控制台启动失败：{exc}，可通过 --port 指定其他端口\n')
        return
    from modules.device_adapter import DeviceAdapter
    device = DeviceAdapter('hmdriver2' if args.device == 'harmony' else 'uiautomator2', args.serial)
    runner = CampaignRunner(device, config)
    try:
        result = runner.scan(args.auto_open) if args.scan else runner.run(args.auto_open)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (NeedsAttention, Stopped) as exc:
        parser.exit(2, f'{exc}\n')
    except KeyboardInterrupt:
        runner.control.stopped.set()
        print('\n已停止')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        logging.exception('启动失败')
        if getattr(sys, 'frozen', False):
            import ctypes
            from modules.runtime import data_dir
            ctypes.windll.user32.MessageBoxW(0, f'启动失败，请查看日志：\n{data_dir() / "application.log"}', '喵任务', 16)
        raise
