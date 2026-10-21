"""Prefer readable controls; visual verification skips redundant hierarchy RPCs."""
import copy
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import logging
import os
import re
import time

from modules.task_model import TextNode, bounds_of, normalize

logger = logging.getLogger(__name__)

# The detector runs fastest near a 1000px short edge, which suits phones.
# Tablets and wide screens expose the same captions on far more pixels, so
# shrinking them to 1000px blurs small text; keep more of their detail.
PHONE_OCR_EDGE = 1000
LARGE_OCR_EDGE = 1600
PHONE_MAX_EDGE = 1200


def ocr_scale_for(width, height):
    """Adaptive OCR input size, measured on the stable short edge.

    A rotated tablet reports its long side as width, so the short edge keeps
    portrait and landscape at the same effective text size. Phones keep the
    classic compact input; larger screens keep more pixels, never upscaled.
    """
    short = min(width, height)
    if short <= 0:
        return 1.0
    target = PHONE_OCR_EDGE if short <= PHONE_MAX_EDGE else LARGE_OCR_EDGE
    return min(1.0, min(short, target) / short)


@lru_cache(maxsize=1)
def local_ocr_engine():
    try:
        from rapidocr import RapidOCR
    except ImportError as exc:
        raise RuntimeError('页面未提供可读控件，请先运行 pip install -r requirements.txt 安装本地 OCR') from exc
    engine = RapidOCR(params={
        'EngineConfig.onnxruntime.intra_op_num_threads': min(4, os.cpu_count() or 2),
        'EngineConfig.onnxruntime.inter_op_num_threads': 1,
        'Global.log_level': 'warning',
    })
    # The locked RapidOCR version exposes this recognizer protocol. If a source
    # install uses an incompatible release, retain its standard OCR path.
    try:
        from modules.ocr_cache import ExactTextCache
        engine.text_rec = ExactTextCache(engine._load_rec_model())
    except (AttributeError, ImportError):
        logger.info('当前 OCR 版本不支持文字块缓存，使用标准识别')
    return engine


@dataclass
class Screen:
    nodes: list
    width: int
    height: int
    package: str
    source: str = "ui"
    tree: object = None

    @property
    def text(self):
        return "\n".join(n.text for n in self.nodes)

    def find(self, pattern, exact=False):
        for node in self.nodes:
            if (normalize(node.text) == normalize(pattern) if exact else
                    re.search(pattern, normalize(node.text))):
                return node
        return None


class ScreenReader:
    def __init__(self, device, ocr=True, on_image=None):
        self.device = device
        self.ocr_enabled = ocr
        self.on_image = on_image
        self._engine = None
        self._ocr_notice = False
        self._size = None
        self.last_image = None
        self.last_screen = None
        self._last_preview = 0
        self._ocr_cache_key = None
        self._ocr_cache_nodes = None
        self._scale_notice = False
        self.last_observation_seconds = 0.0

    def _flatten(self, tree):
        nodes = []
        def walk(item):
            if isinstance(item, list):
                for child in item:
                    walk(child)
            elif isinstance(item, dict):
                attrs = item.get("attributes", item)
                package = attrs.get("package", "")
                if not package.startswith(("com.android.systemui", "com.miui.notes", "com.google.android.inputmethod")):
                    text = attrs.get("text") or attrs.get("description") or attrs.get("content-desc") or ""
                    bounds = bounds_of(attrs.get("bounds", attrs.get("rect")))
                    if text and bounds[2] > bounds[0] and bounds[3] > bounds[1]:
                        nodes.append(TextNode(text, bounds))
                for key in ("children", "hierarchy"):
                    if isinstance(item.get(key), (list, dict)):
                        walk(item[key])
        walk(tree)
        return nodes

    def _scale_for(self, image):
        scale = ocr_scale_for(image.width, image.height)
        if not self._scale_notice:
            self._scale_notice = True
            logger.info('屏幕 %dx%d，本地 OCR 输入短边约 %dpx（缩放 %.2f）',
                        image.width, image.height, round(min(image.size) * scale), scale)
        return scale

    def _ocr(self, image):
        if self._engine is None:
            self._engine = local_ocr_engine()
        import numpy as np
        # Keep enough pixels for this screen size, then map boxes back to it.
        scale = self._scale_for(image)
        resized = image if scale >= 1 else image.resize((round(image.width * scale), round(image.height * scale)))
        result = self._engine(np.asarray(resized), use_cls=False)
        results = zip(result.boxes, result.txts, result.scores) if result.boxes is not None else []
        nodes = []
        for box, text, confidence in results:
            if confidence < .65:
                continue
            xs, ys = zip(*box)
            bounds = tuple(round(v / scale) for v in (min(xs), min(ys), max(xs), max(ys)))
            nodes.append(TextNode(text, bounds, "ocr"))
        return nodes

    def _image_nodes(self, image):
        # Include every original RGB pixel and the dimensions. A thumbnail or
        # perceptual hash can miss a changed countdown or modal and is unsafe.
        key = (image.size, hashlib.sha256(image.tobytes()).digest())
        if key == self._ocr_cache_key:
            return copy.deepcopy(self._ocr_cache_nodes)
        nodes = self._ocr(image)
        self._ocr_cache_key = key
        self._ocr_cache_nodes = copy.deepcopy(nodes)
        return nodes

    def _capture(self):
        image = self.device.screenshot(max_age=.15).convert('RGB')
        self.last_image = image
        self._size = image.size
        if self.on_image:
            self.on_image(image)
        self._last_preview = time.monotonic()
        return image

    def _observed_size(self, nodes):
        """Trust the control tree over a stale orientation reported by the driver.

        Some tablets report a rotated window_size while dump_hierarchy keeps the
        other orientation. When the bounds clearly exceed the reported panel and
        fit after swapping, use the swapped dimensions.
        """
        try:
            width, height = self.device.display_size
        except Exception:
            return (1440, 3200)
        if nodes:
            right = max(n.bounds[2] for n in nodes)
            bottom = max(n.bounds[3] for n in nodes)
            if right > width * 1.02 or bottom > height * 1.02:
                if right <= height * 1.02 and bottom <= width * 1.02:
                    logger.info('控件树方向与系统尺寸不一致，改用 %sx%s', height, width)
                    return (height, width)
        return (width, height)

    def enrich_regions(self, screen, regions):
        """OCR crops of this exact observed frame, keeping full-frame guards."""
        if not self.ocr_enabled or self.last_screen is not screen or self.last_image is None:
            return screen
        nodes = list(screen.nodes)
        for left, top, right, bottom in regions:
            left, top = max(0, left), max(0, top)
            right, bottom = min(screen.width, right), min(screen.height, bottom)
            if left >= right or top >= bottom:
                continue
            for node in self._ocr(self.last_image.crop((left, top, right, bottom))):
                x1, y1, x2, y2 = node.bounds
                nodes.append(TextNode(node.text, (x1+left, y1+top, x2+left, y2+top), 'ocr-region'))
        enriched = Screen(nodes, screen.width, screen.height, screen.package, 'ocr+regions', screen.tree)
        self.last_screen = enriched
        return enriched

    def read(self, force_ocr=False):
        started = time.monotonic()
        direct_visual = bool(force_ocr and self.ocr_enabled)
        tree = None if direct_visual else self.device.dump_hierarchy()
        package = self.device.current_app().get("package", "")
        nodes = [] if direct_visual else self._flatten(tree)
        meaningful = [n for n in nodes if n.bounds[3] - n.bounds[1] < (self._size or (1440, 3200))[1] * .4]
        source = "ui"
        if self.ocr_enabled and (force_ocr or len(meaningful) < 5):
            if not self._ocr_notice:
                logger.info("页面控件不可读，启用本地 OCR 识别（截图不上传）")
                self._ocr_notice = True
            image = self._capture()
            nodes = self._image_nodes(image)
            source = "ocr"
        elif (self.on_image and not getattr(self.device, 'preview_managed', False)
              and time.monotonic() - self._last_preview >= 1):
            self._capture()
        if self._size is None:
            self._size = self._observed_size(nodes)
        screen = Screen(nodes, *self._size, package, source, tree)
        self.last_screen = screen
        self.last_observation_seconds = time.monotonic() - started
        return screen
