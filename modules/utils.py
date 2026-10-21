"""Activity entry helpers derived from MonsterNone/tmall-miao, next branch."""
import time


def _get_taobao_pkg(device):
    return 'com.taobao.taobao4hmos' if device.device_type == 'hmdriver2' else 'com.taobao.taobao'


def open_taobao_search(device, text, control=None):
    """Search from a visible homepage entry without terminating the app."""
    from modules.screen_reader import ScreenReader
    reader = ScreenReader(device)
    wait = control.wait if control else time.sleep
    check = control.check if control else lambda: None
    check()
    device.resume_app(_get_taobao_pkg(device))
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        check()
        screen = reader.read()
        search = screen.find(r'^搜索(?:栏|框|商品)$')
        if search:
            check()
            device.click(*search.center)
            break
        wait(.5)
    else:
        raise RuntimeError('未找到淘宝首页搜索入口，请手动进入活动任务列表')
    wait(.6)
    check()
    device.input_text(text)
    screen = reader.read()
    buttons = [node for node in screen.nodes if node.text == '搜索'
               and node.center[0] > screen.width * .7 and node.center[1] < screen.height * .22
               and node.bounds[2] - node.bounds[0] < screen.width * .3]
    if not buttons:
        raise RuntimeError('未找到搜索按钮，请手动进入活动任务列表')
    check()
    device.click(*max(buttons, key=lambda node: node.center[0]).center)
