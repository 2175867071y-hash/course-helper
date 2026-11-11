"""用真实浏览器滚动和截图复现 OCR 裁剪误判；不调用外部 AI。"""
import struct
import math
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from browser import ManualRequired
from quiz_ocr import read_text


class OCRLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='msedge', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page(viewport={'width': 1100, 'height': 650})

    def tearDown(self):
        self.page.close()

    def test_root_overflow_does_not_reject_visible_scrolled_iframe_text(self):
        for doctype, root_css in (('', 'body'), ('<!doctype html>', 'html')):
            with self.subTest(root=root_css):
                self.page.set_content(doctype + '<style>' + root_css +
                                      '{overflow:hidden}</style><iframe '
                                      'style="width:95%;height:2000px"></iframe>')
                self.page.frames[1].set_content(
                    '<iframe style="width:95%;height:1900px"></iframe>')
                self.page.frames[2].set_content(
                    '<iframe style="width:95%;height:1800px"></iframe>')
                frame = self.page.frames[3]
                frame.set_content('<div style="height:850px"></div>'
                                  '<p style="font:24px Microsoft YaHei">语音识别（ASR）</p>')
                with patch('quiz_ocr._recognize', return_value='语音识别（ASR）') as recognize:
                    self.assertEqual(read_text(frame.locator('p')), '语音识别（ASR）')
                recognize.assert_called_once()
                self.assertGreater(self.page.evaluate('scrollY'), 0)
                box = frame.locator('p').bounding_box()
                self.assertGreaterEqual(box['y'], 0)
                self.assertLessEqual(box['y'] + box['height'], 650)

    def test_wide_empty_container_does_not_make_visible_text_incomplete(self):
        self.page.set_viewport_size({'width': 500, 'height': 350})
        self.page.set_content('<div style="width:1600px;font:24px Microsoft YaHei">'
                              '语音识别（ASR）</div>')
        with patch('quiz_ocr._recognize', return_value='语音识别（ASR）') as recognize:
            self.assertEqual(read_text(self.page.locator('div')), '语音识别（ASR）')
        png = recognize.call_args.args[0]
        width, height = struct.unpack('>II', png[16:24])
        self.assertLess(width, 500)
        self.assertLess(height, 100)

    def test_text_wider_than_viewport_still_stops_before_ocr(self):
        self.page.set_viewport_size({'width': 400, 'height': 350})
        self.page.set_content('<div style="width:1600px;white-space:nowrap;font:24px Arial">'
                              + 'Important condition ' * 15 + '</div>')
        with patch('quiz_ocr._recognize') as recognize:
            with self.assertRaisesRegex(ManualRequired, '裁剪'):
                read_text(self.page.locator('div'))
        recognize.assert_not_called()

    def test_body_with_own_clipping_is_not_treated_as_viewport(self):
        self.page.set_content('<!doctype html><style>html{overflow:auto}'
                              'body{height:40px;overflow:hidden;margin:0}'
                              'p{margin:0;font:24px Arial;line-height:40px}</style>'
                              '<p>First condition<br>Second condition<br>Last condition</p>')
        with patch('quiz_ocr._recognize') as recognize:
            with self.assertRaisesRegex(ManualRequired, '裁剪'):
                read_text(self.page.locator('p'))
        recognize.assert_not_called()

    def test_containment_keeps_body_clipping_separate_from_viewport(self):
        for target in ('html', 'body'):
            with self.subTest(containment=target):
                self.page.set_content('<!doctype html><style>' + target +
                                      '{contain:paint}body{height:40px;overflow:hidden;margin:0}'
                                      'p{margin:0;font:24px Arial;line-height:40px}</style>'
                                      '<p>First condition<br>Second condition<br>Last condition</p>')
                with patch('quiz_ocr._recognize') as recognize:
                    with self.assertRaisesRegex(ManualRequired, '裁剪'):
                        read_text(self.page.locator('p'))
                recognize.assert_not_called()

    def test_text_bounds_do_not_omit_generated_question_conditions(self):
        self.page.set_viewport_size({'width': 600, 'height': 400})
        for pseudo, condition in (('before', 'DO NOT '), ('after', ' except C')):
            with self.subTest(pseudo=pseudo):
                self.page.set_content('<style>p{width:1600px;font:28px Arial}'
                                      'p::' + pseudo + '{content:"' + condition + '"}</style>'
                                      '<p>Select all answers</p>')
                with patch('quiz_ocr._recognize') as recognize:
                    with self.assertRaisesRegex(ManualRequired, '裁剪'):
                        read_text(self.page.locator('p'))
                recognize.assert_not_called()

    def test_short_answer_crop_keeps_visible_generated_conditions(self):
        self.page.set_content('<style>p{width:900px;font:24px Arial}'
                              'p::before{content:"NOT "}</style><p>A</p>')
        with patch('quiz_ocr._recognize', return_value='NOT A') as recognize:
            self.assertEqual(read_text(self.page.locator('p')), 'NOT A')
        width, _ = struct.unpack('>II', recognize.call_args.args[0][16:24])
        self.assertEqual(width, 900)

    def test_frame_padding_is_included_in_the_captured_text_position(self):
        self.page.set_content('<iframe style="width:500px;height:300px;'
                              'border:10px solid red;padding:20px"></iframe>')
        frame = self.page.frames[1]
        frame.set_content('<style>body{margin:0}p{margin:0;font:28px Arial;'
                          'line-height:40px}</style><p>First condition</p>')
        node = frame.locator('p')
        with patch.object(self.page, 'screenshot', wraps=self.page.screenshot) as capture:
            with patch('quiz_ocr._recognize', return_value='First condition'):
                self.assertEqual(read_text(node), 'First condition')
        box = node.bounding_box()
        clip = capture.call_args.kwargs['clip']
        self.assertEqual(clip['x'], math.floor(box['x']))
        self.assertEqual(clip['y'], math.floor(box['y']))
        self.assertEqual(clip['width'], math.ceil(box['x']+box['width'])-clip['x'])
        self.assertEqual(clip['height'], math.ceil(box['y']+box['height'])-clip['y'])

    def test_fractional_frame_width_does_not_shift_screenshot_by_one_pixel(self):
        self.page.set_content('<iframe style="width:90%;height:1800px"></iframe>')
        frame = self.page.frames[1]
        frame.set_content('<div style="height:850px"></div>'
                          '<p style="font:28px Arial;width:600px">Speech recognition (ASR)</p>')
        node = frame.locator('p')
        with patch.object(self.page, 'screenshot', wraps=self.page.screenshot) as capture:
            with patch('quiz_ocr._recognize', return_value='Speech recognition (ASR)'):
                read_text(node)
        box = node.bounding_box()
        clip = capture.call_args.kwargs['clip']
        self.assertEqual(clip['x'], math.floor(box['x']))
        self.assertEqual(clip['y'], math.floor(box['y']))
        self.assertEqual(clip['width'], math.ceil(box['x']+box['width'])-clip['x'])
        self.assertEqual(clip['height'], math.ceil(box['y']+box['height'])-clip['y'])


if __name__ == '__main__':
    unittest.main()
