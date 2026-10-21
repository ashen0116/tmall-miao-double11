"""
设备适配器层 - 统一hmdriver2和uiautomator2的API

通过此适配器，业务代码无需关心底层驱动差异，
使用统一的API操作鸿蒙(HarmonyOS)和安卓(Android)设备。
"""
import logging
import re
from contextlib import nullcontext
from functools import wraps
import shlex
import tempfile
import threading
import time
from pathlib import Path
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


def serialized(method):
    """Keep preview captures and task RPCs on one device from interleaving."""
    @wraps(method)
    def call(self, *args, **kwargs):
        with getattr(self, 'io_lock', None) or nullcontext():
            mutating = method.__name__ not in {'screenshot', 'current_app', 'dump_hierarchy', 'installed_packages'}
            if mutating:
                self._screenshot_cache = None
            try:
                return method(self, *args, **kwargs)
            finally:
                if mutating:
                    self._screenshot_cache = None
    return call


class DeviceAdapter:
    """设备适配器，统一HarmonyOS和Android的UI自动化接口"""

    def __init__(self, device_type, serial=None, on_status=None):
        """
        初始化设备适配器

        Args:
            device_type: 'hmdriver2' 或 'uiautomator2'（必填）
            serial: 设备序列号或无线连接地址（如 '192.168.1.100:5555'）
        """
        self.device_type = device_type
        self.serial = serial
        self.io_lock = threading.RLock()
        self.on_status = on_status or logger.info
        self._device = None
        self._last_xml = None  # 缓存uiautomator2的原始XML，避免json2xml重复调用
        self._init_device()

    def _init_device(self):
        """初始化底层设备驱动"""
        if self.device_type == 'hmdriver2':
            import hmdriver2
            from hmdriver2.driver import Driver
            hmdriver2.driver.logger.disabled = False
            if self.serial and ':' in self.serial:
                # 无线连接：先通过 hdc tconn 连接远程设备
                import subprocess
                logger.info(f'通过WiFi连接鸿蒙设备: {self.serial}')
                result = subprocess.run(
                    ['hdc', 'tconn', self.serial],
                    capture_output=True, text=True, timeout=10
                )
                if 'Connect OK' not in result.stdout and result.returncode != 0:
                    raise ConnectionError(
                        f'hdc tconn {self.serial} 失败: {result.stderr or result.stdout}\n'
                        f'请确保: 1. 先用USB执行 hdc tmode port 5555 开启TCP模式\n'
                        f'        2. 设备与电脑在同一局域网'
                    )
                logger.info(f'WiFi连接成功: {self.serial}')
            self._device = Driver(serial=self.serial)
        elif self.device_type == 'uiautomator2':
            import uiautomator2 as u2
            from modules.adb_transport import prepare_android_connection
            self.serial = prepare_android_connection(self.serial, on_status=self.on_status)
            # 设置HTTP超时（模块级常量）
            u2.HTTP_TIMEOUT = 20
            if self.serial:
                # serial 可以是 IP:PORT（无线）或设备序列号（USB）
                logger.info(f'连接安卓设备: {self.serial}')
                self._device = u2.connect(self.serial)
            else:
                self._device = u2.connect()
            # 设置默认等待超时
            self._device.settings['wait_timeout'] = 3
            self._device.settings['operation_delay'] = (0.1, 0.1)
            self.identity_serial = self._android_identity() or self.serial
            # Automation dies silently on a dark display; tablets often sleep
            # within a minute. This temporary setting reverts on unplug/reboot.
            self.keep_awake_while_plugged()
        else:
            raise ValueError(f"不支持的设备类型: {self.device_type}")

    def _android_identity(self):
        """Keep USB and Wi-Fi transports of the same handset in one daily record."""
        for prop in ('ro.serialno', 'ro.boot.serialno'):
            try:
                result = self._device.shell(['getprop', prop])
                value = result.output.strip()
                if (result.exit_code == 0 and re.fullmatch(r'[A-Za-z0-9._-]{3,128}', value)
                        and value.lower() not in ('unknown', 'null', 'none')):
                    return value
            except Exception:
                logger.debug('无法读取设备持久标识，使用连接标识', exc_info=True)
        return None

    @property
    def device(self):
        """获取底层设备对象"""
        return self._device

    def xpath(self, xpath_str):
        """XPath定位，返回兼容的选择器对象"""
        return self._device.xpath(xpath_str)

    def xpath_exists(self, xpath_str):
        """
        XPath判断元素是否存在（兼容hmdriver2和uiautomator2）

        hmdriver2: d.xpath('xxx').exists() 是方法调用
        uiautomator2: d.xpath('xxx').exists 是属性（bool值）
        """
        if self.device_type == 'hmdriver2':
            return self._device.xpath(xpath_str).exists()
        else:
            return self._device.xpath(xpath_str).exists

    def __call__(self, **kwargs):
        """选择器定位，支持 text, resourceId, className 等"""
        return self._device(**kwargs)

    def click_if_exists(self, **kwargs):
        """
        如果元素存在则点击（兼容hmdriver2和uiautomator2）

        hmdriver2的selector有click_if_exists()方法
        uiautomator2的selector没有，需先exists()再click()
        """
        if self.device_type == 'hmdriver2':
            self._device(**kwargs).click_if_exists()
        else:
            obj = self._device(**kwargs)
            if obj.exists():
                obj.click()

    @serialized
    def click(self, x, y):
        """点击坐标"""
        self._device.click(x, y)

    @serialized
    def swipe(self, x1, y1, x2, y2, duration=0.5):
        """滑动"""
        if self.device_type == 'hmdriver2':
            self._device.swipe(x1=x1, y1=y1, x2=x2, y2=y2)
        else:
            # uiautomator2: swipe(fx, fy, tx, ty, duration)
            self._device.swipe(x1, y1, x2, y2, duration=duration)

    @serialized
    def swipe_ext(self, direction, box=None):
        """方向滑动"""
        if box:
            self._device.swipe_ext(direction, box=box)
        else:
            self._device.swipe_ext(direction)

    @serialized
    def is_screen_on(self):
        """Return True when the display is on; unknown drivers count as on.

        uiautomator2's cached info reports screenOn=True for asleep HyperOS
        tablets, so ask the system power service for the real state.
        """
        if self.device_type == 'uiautomator2':
            try:
                result = self._device.shell('dumpsys power | grep mWakefulness=')
                output = getattr(result, 'output', result)
                if isinstance(output, str) and 'mWakefulness=' in output:
                    return 'Awake' in output
            except Exception:
                logger.debug('无法通过 dumpsys 读取屏幕状态', exc_info=True)
            try:
                return bool(self._device.info.get('screenOn', True))
            except Exception:
                return True
        return True

    @serialized
    def wake_screen(self):
        """Wake the display, dismiss a keyless lockscreen, then swipe up.

        A password lock stays on its input page; the runner saves evidence and
        asks the user to unlock instead of guessing credentials.
        """
        if self.device_type == 'uiautomator2':
            try:
                self._device.shell(['input', 'keyevent', 'KEYCODE_WAKEUP'])
                self._device.shell(['wm', 'dismiss-keyguard'])
            except Exception:
                logger.debug('唤醒屏幕失败', exc_info=True)
        else:
            try:
                # KEYCODE_WAKEUP; HarmonyOS keeps uitest's keyEvent injection.
                self._device.shell('uitest uiInput keyEvent 224')
            except Exception:
                logger.debug('鸿蒙唤醒屏幕失败', exc_info=True)
            return
        try:
            width, height = self.display_size
            self._device.swipe(width * .5, height * .9, width * .5, height * .3, duration=.3)
        except Exception:
            logger.debug('解锁滑动未执行，等待本人处理锁屏', exc_info=True)

    @serialized
    def keep_awake_while_plugged(self):
        """Keep the display on while charging over USB (temporary, not persisted)."""
        if self.device_type != 'uiautomator2':
            return
        try:
            self._device.shell(['svc', 'power', 'stayon', 'usb'])
        except Exception:
            logger.debug('设置充电保持亮屏失败，请手动保持屏幕常亮', exc_info=True)

    @serialized
    def go_back(self):
        """返回"""
        if self.device_type == 'hmdriver2':
            self._device.go_back()
        else:
            self._device.press("back")

    @serialized
    def go_home(self):
        """回到主页"""
        if self.device_type == 'hmdriver2':
            self._device.go_home()
        else:
            self._device.press("home")

    @serialized
    def input_text(self, text):
        """输入文本"""
        if self.device_type == 'hmdriver2':
            self._device.input_text(text)
        else:
            # The native editable node supports Unicode directly. send_keys /
            # clear_text may install and switch IMEs and then retry broadcasts.
            edit = self._device(className='android.widget.EditText')
            if edit.count == 1:
                edit.set_text(text, timeout=.25)
            else:
                self._device(className='android.widget.EditText', focused=True).set_text(text, timeout=.25)

    @serialized
    def clear_text(self):
        """清除文本"""
        if self.device_type == 'hmdriver2':
            self._device.input_text("")
        else:
            self._device(focused=True).set_text('')

    @serialized
    def start_app(self, package_name, activity=None):
        """启动应用"""
        if self.device_type == 'hmdriver2':
            self._device.start_app(package_name)
        else:
            self._device.app_start(package_name, activity=activity)

    @serialized
    def stop_app(self, package_name):
        """停止应用"""
        if self.device_type == 'hmdriver2':
            self._device.stop_app(package_name)
        else:
            self._device.app_stop(package_name)

    @serialized
    def shell(self, cmd):
        """执行shell命令"""
        if self.device_type == 'hmdriver2':
            return self._device.shell(cmd)
        else:
            if isinstance(cmd, str):
                cmd = shlex.split(cmd)
            return self._device.shell(cmd)

    @serialized
    def current_app(self):
        """Return a common package/activity mapping on both drivers."""
        if self.device_type == 'hmdriver2':
            package, activity = self._device.current_app()
            info = {'package': package or '', 'activity': activity or ''}
        else:
            info = self._device.app_current()
        package = info.get('package', '')
        if package != getattr(self, '_foreground_package', None):
            self._screenshot_cache = None
            self._foreground_package = package
        return info

    @serialized
    def installed_packages(self):
        """Return installed Android packages, or None when detection is unavailable."""
        if self.device_type != 'uiautomator2':
            return None
        result = self._device.shell(['pm', 'list', 'packages'])
        if result.exit_code != 0:
            return None
        return {line.removeprefix('package:').strip() for line in result.output.splitlines()
                if line.startswith('package:')}

    @serialized
    def resume_app(self, package_name):
        """Bring the existing application task forward without force stopping it."""
        if self.device_type == 'hmdriver2':
            self._device.start_app(package_name)
        else:
            # Supplying the launcher activity avoids u2's monkey fallback, which
            # can change rotation lock settings. Android restores the existing task.
            result = self._device.shell(['cmd', 'package', 'resolve-activity', '--brief',
                                         '-a', 'android.intent.action.MAIN', '-c',
                                         'android.intent.category.LAUNCHER', package_name])
            component = result.output.strip().splitlines()[-1]
            if '/' not in component or ' ' in component:
                raise RuntimeError(f'无法找到应用启动入口：{package_name}')
            self._device.app_start(package_name, activity=component.split('/', 1)[1], stop=False)

    @serialized
    def restart_app(self, package_name):
        """Cold-start for fresh desktop entry or a bounded recovery attempt."""
        self.stop_app(package_name)
        self.resume_app(package_name)

    @serialized
    def screenshot(self, max_age=0):
        """Return a Pillow image; keep Harmony's temporary file off disk afterwards."""
        cached = getattr(self, '_screenshot_cache', None)
        if max_age > 0 and cached is not None and time.monotonic() - cached[0] <= min(max_age, .15):
            return cached[1].copy()
        started = time.monotonic()
        self._screenshot_cache = None
        if self.device_type == 'uiautomator2':
            try:
                image = self._device.screenshot()
            except OSError:
                # Cold launch can momentarily close the automation RPC socket.
                # Explicit primary display uses u2's public direct-ADB capture,
                # returning a fresh frame without another app/server restart.
                logger.debug('截图服务暂时不可用，改用 ADB 截图', exc_info=True)
                image = self._device.screenshot(display_id=0)
        else:
            from PIL import Image
            with tempfile.TemporaryDirectory(prefix='miao-screen-') as folder:
                path = str(Path(folder) / 'screen.png')
                self._device.screenshot(path)
                with Image.open(path) as source:
                    image = source.convert('RGB').copy()
        if hasattr(image, 'copy'):
            self._screenshot_cache = (started, image.copy())
        return image

    @serialized
    def dump_hierarchy(self):
        """
        获取UI层级结构，统一返回JSON dict

        hmdriver2 原生返回 JSON dict
        uiautomator2 返回 XML string，需转换为 JSON dict（属性名映射为兼容格式）
        """
        if self.device_type == 'hmdriver2':
            return self._device.dump_hierarchy()
        else:
            # uiautomator2 返回XML，缓存原始XML并转为兼容JSON
            self._last_xml = self._device.dump_hierarchy(compressed=True)
            return self._xml_to_json(self._last_xml)

    # XML属性名到hmdriver2 JSON key的映射
    _XML_ATTR_MAP = {
        'resource-id': 'id',
        'class': 'type',
        'content-desc': 'description',
        'long-clickable': 'longClickable',
        'scrollable': 'scrollable',
        'password': 'password',
    }

    def _xml_to_json(self, xml_str):
        """将uiautomator2的XML层级转为与hmdriver2兼容的JSON格式"""
        root = ET.fromstring(xml_str)
        return self._xml_element_to_dict(root)

    def _xml_element_to_dict(self, element):
        """递归将XML元素转为dict，属性名映射为hmdriver2兼容格式"""
        result = {}
        # 将XML属性映射为dict的key（兼容hmdriver2的JSON格式）
        for key, value in element.attrib.items():
            mapped_key = self._XML_ATTR_MAP.get(key, key)
            result[mapped_key] = value

        # 递归处理子元素
        children = list(element)
        if children:
            result['children'] = [self._xml_element_to_dict(child) for child in children]

        return result

    @property
    def display_size(self):
        """获取屏幕尺寸 (width, height)"""
        if self.device_type == 'hmdriver2':
            return self._device.display_size
        else:
            w, h = self._device.window_size()
            return (w, h)

    @property
    def gesture(self):
        """手势操作（仅hmdriver2支持）"""
        if self.device_type == 'hmdriver2':
            return self._device.gesture
        else:
            raise NotImplementedError("uiautomator2不支持gesture API，请使用swipe方法")

    @serialized
    def press(self, key):
        """按键"""
        if self.device_type == 'hmdriver2':
            if key == 'back':
                self._device.go_back()
            elif key == 'home':
                self._device.go_home()
            else:
                self._device.press_key(key)
        else:
            self._device.press(key)

    def mute(self):
        """静音：多次按音量减"""
        if self.device_type == 'hmdriver2':
            for _ in range(10):
                self._device.shell("uitest uiInput keyEvent 17")
        else:
            for _ in range(10):
                self._device.press("volume_down")

    def json2xml(self, hierarchy):
        """
        将hierarchy转为XML字符串（用于xpath查找）

        hmdriver2: 使用内置_XPath._json2xml
        uiautomator2: 使用缓存的原始XML，避免重复RPC调用
        """
        if self.device_type == 'hmdriver2':
            from hmdriver2._xpath import _XPath
            return _XPath._json2xml(hierarchy)
        else:
            # 优先使用缓存的XML，避免再次调用dump_hierarchy
            if self._last_xml is not None:
                return self._last_xml
            return self._device.dump_hierarchy()

    def __repr__(self):
        conn = f", serial={self.serial}" if self.serial else ""
        return f"DeviceAdapter(type={self.device_type}{conn})"


def create_device(device_type, serial=None):
    """工厂函数，创建设备适配器"""
    return DeviceAdapter(device_type, serial)
