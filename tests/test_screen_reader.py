import unittest
from unittest.mock import Mock, patch

from PIL import Image

from modules.screen_reader import ScreenReader, ocr_scale_for
from modules.task_model import TextNode


class AdaptiveOcrScaleTests(unittest.TestCase):
    """Phones stay compact; tablets keep enough pixels for small captions."""

    def test_phone_portrait_and_landscape_share_one_input_size(self):
        self.assertAlmostEqual(ocr_scale_for(1080, 2400), 1000 / 1080, places=3)
        self.assertAlmostEqual(ocr_scale_for(2400, 1080), 1000 / 1080, places=3)

    def test_tablet_keeps_more_pixels_than_the_phone_limit(self):
        scale = ocr_scale_for(2032, 3048)
        self.assertAlmostEqual(scale, 1600 / 2032, places=3)
        self.assertGreater(min(2032, 3048) * scale, 1500)

    def test_rotated_tablet_uses_the_stable_short_edge(self):
        self.assertEqual(ocr_scale_for(3048, 2032), ocr_scale_for(2032, 3048))

    def test_small_crops_are_never_upscaled(self):
        self.assertEqual(ocr_scale_for(800, 400), 1.0)
        self.assertEqual(ocr_scale_for(0, 0), 1.0)


def readable_tree():
    return {'hierarchy': {'children': [
        {'text': f'控件 {index}', 'bounds': [10, 70 + index * 90, 400, 120 + index * 90]}
        for index in range(6)
    ]}}


def device():
    result = Mock(preview_managed=False)
    result.dump_hierarchy.return_value = readable_tree()
    result.current_app.return_value = {'package': 'com.taobao.taobao'}
    result.screenshot.return_value = Image.new('RGB', (500, 1000), 'white')
    result.display_size = (500, 1000)
    return result


class ScreenReaderObservationTests(unittest.TestCase):
    def test_force_ocr_skips_hierarchy_rpc_and_keeps_latest_image(self):
        phone = device()
        on_image = Mock()
        reader = ScreenReader(phone, on_image=on_image)
        reader._ocr = Mock(return_value=[TextNode('前台对话框', (10, 10, 300, 100), 'ocr')])
        screen = reader.read(force_ocr=True)
        phone.dump_hierarchy.assert_not_called()
        phone.screenshot.assert_called_once()
        phone.current_app.assert_called_once()
        self.assertEqual(screen.source, 'ocr')
        self.assertIsNone(screen.tree)
        self.assertEqual(screen.find('前台对话框').text, '前台对话框')
        self.assertEqual(reader.last_image.size, (500, 1000))
        self.assertIs(reader.last_screen, screen)
        on_image.assert_called_once_with(reader.last_image)
        self.assertGreaterEqual(reader.last_observation_seconds, 0)

    def test_readable_ui_keeps_original_tree_for_search_without_ocr(self):
        phone = device()
        reader = ScreenReader(phone)
        reader._ocr = Mock()
        screen = reader.read()
        phone.dump_hierarchy.assert_called_once()
        phone.screenshot.assert_not_called()
        reader._ocr.assert_not_called()
        self.assertIs(screen.tree, phone.dump_hierarchy.return_value)
        self.assertEqual(screen.source, 'ui')
        self.assertEqual(len(screen.nodes), 6)

    def test_managed_preview_avoids_duplicate_capture_but_not_visual_verification(self):
        phone = device()
        phone.preview_managed = True
        on_image = Mock()
        reader = ScreenReader(phone, on_image=on_image)
        reader.read()
        phone.screenshot.assert_not_called()
        on_image.assert_not_called()
        reader._ocr = Mock(return_value=[])
        reader.read(force_ocr=True)
        phone.screenshot.assert_called_once()
        on_image.assert_called_once()

    def test_unmanaged_preview_refreshes_after_one_second_and_sets_last_image(self):
        phone = device()
        on_image = Mock()
        reader = ScreenReader(phone, on_image=on_image)
        with patch('modules.screen_reader.time.monotonic', return_value=10):
            reader.read()
        with patch('modules.screen_reader.time.monotonic', return_value=10.8):
            reader.read()
        self.assertEqual(phone.screenshot.call_count, 1)
        with patch('modules.screen_reader.time.monotonic', return_value=11.01):
            reader.read()
        self.assertEqual(phone.screenshot.call_count, 2)
        self.assertEqual(reader.last_image.size, (500, 1000))

    def test_pixel_identical_frame_reuses_only_last_ocr_result_and_does_not_share_nodes(self):
        phone = device()
        on_image = Mock()
        reader = ScreenReader(phone, on_image=on_image)
        reader._ocr = Mock(return_value=[TextNode('初始识别', (10, 10, 200, 100), 'ocr')])
        first = reader.read(force_ocr=True)
        first.nodes[0].text = '调用方改了当前结果'
        second = reader.read(force_ocr=True)
        self.assertEqual(reader._ocr.call_count, 1)
        self.assertEqual(second.nodes[0].text, '初始识别')
        self.assertEqual(on_image.call_count, 2)
        # A one-pixel change outside the OCR thumbnail must invalidate the cache.
        different = phone.screenshot.return_value.copy()
        different.putpixel((499, 999), (1, 2, 3))
        phone.screenshot.return_value = different
        reader.read(force_ocr=True)
        self.assertEqual(reader._ocr.call_count, 2)
        # This is a one-image cache, so returning to the original also re-runs OCR.
        phone.screenshot.return_value = Image.new('RGB', (500, 1000), 'white')
        reader.read(force_ocr=True)
        self.assertEqual(reader._ocr.call_count, 3)

    def test_equal_byte_stream_with_different_dimensions_does_not_hit_ocr_cache(self):
        phone = device()
        phone.screenshot.return_value = Image.new('RGB', (40, 20), 'white')
        reader = ScreenReader(phone)
        reader._ocr = Mock(return_value=[])
        reader.read(force_ocr=True)
        phone.screenshot.return_value = Image.new('RGB', (20, 40), 'white')
        reader.read(force_ocr=True)
        self.assertEqual(reader._ocr.call_count, 2)

    def test_disabled_ocr_still_reads_ui_when_force_requested(self):
        phone = device()
        reader = ScreenReader(phone, ocr=False)
        reader._ocr = Mock()
        screen = reader.read(force_ocr=True)
        phone.dump_hierarchy.assert_called_once()
        reader._ocr.assert_not_called()
        self.assertEqual(screen.source, 'ui')

    def test_unreadable_ui_falls_back_to_ocr_and_keeps_tree_for_native_edit_access(self):
        phone = device()
        phone.dump_hierarchy.return_value = {'text': '', 'children': []}
        reader = ScreenReader(phone)
        reader._ocr = Mock(return_value=[TextNode('搜索', (350, 100, 450, 180), 'ocr')])
        screen = reader.read()
        self.assertEqual(screen.source, 'ocr')
        self.assertIs(screen.tree, phone.dump_hierarchy.return_value)
        phone.dump_hierarchy.assert_called_once()
        phone.screenshot.assert_called_once()


if __name__ == '__main__':
    unittest.main()
