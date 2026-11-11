"""上传入口变体回归；网页及回答全部由本地路由提供。"""
import unittest

from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeoutError
from doubao_web import exchange
from quiz_ai import QuizAIError, QuizImage
from test_doubao_web import Control, FIXTURE


class UploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True, channel='msedge')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.control = Control()
        self.stages = []

    def load(self, html):
        self.page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
        self.page.goto('https://www.doubao.com/chat/')

    def run_exchange(self, timeout=.8):
        picture = QuizImage('q_image', b'\x89PNG\r\n\x1a\nEXACT_BYTES')
        try:
            return exchange(self.page, [{'id': 'q', 'images': ['q_image']}], [picture],
                            self.control, timeout=timeout, settle=.05, progress=self.stages.append)
        except BrowserTimeoutError:
            self.fail(f'上传流程仍强制等待旧菜单或文件选择器，阶段：{self.stages[-1]}')

    def assert_delivered(self):
        result = self.run_exchange()
        self.assertEqual(result['answers'][0]['selected_labels'], ['B'])
        self.assertEqual(len(self.page.evaluate('window.sent')), 1)
        self.assertEqual(self.page.locator('input[type=file]').first.evaluate('(e)=>e.files[0].name'),
                         'image_001.png')

    def test_plus_can_open_file_chooser_directly(self):
        html = FIXTURE.replace("document.querySelector('#menu').hidden=false",
                               "document.querySelector('input').click()")
        self.load(html)
        self.assert_delivered()

    def test_image_only_menu_can_upload(self):
        self.load(FIXTURE.replace('上传文件或图片', '上传图片'))
        self.assert_delivered()

    def test_native_composer_input_can_upload_without_menu(self):
        html = FIXTURE.replace('<button data-testid="upload_file_button"',
                               '<main><button data-testid="upload_file_button"')
        html = html.replace('<section id="responses">', '</main><section id="responses">')
        html = html.replace("document.querySelector('#menu').hidden=false", 'void(0)')
        self.load(html)
        self.assert_delivered()

    def test_unrelated_file_input_is_not_used(self):
        html = FIXTURE.replace("document.querySelector('#menu').hidden=false", 'void(0)')
        self.load(html)
        with self.assertRaisesRegex(QuizAIError, '上传菜单|文件控件'):
            self.run_exchange()
        self.assertEqual(self.page.locator('input').evaluate('(e)=>e.files.length'), 0)
        self.assertEqual(self.page.evaluate('window.sent'), [])

    def test_ambiguous_composer_inputs_do_not_upload(self):
        html = FIXTURE.replace('<button data-testid="upload_file_button"',
                               '<main><button data-testid="upload_file_button"')
        html = html.replace('<section id="responses">', '</main><section id="responses">')
        html = html.replace("document.querySelector('#menu').hidden=false", 'void(0)')
        html = html.replace('<input type="file" multiple hidden>',
                            '<input type="file" multiple hidden><input type="file" accept="image/*" multiple hidden>')
        self.load(html)
        with self.assertRaisesRegex(QuizAIError, '多个.*上传'):
            self.run_exchange()
        self.assertEqual(self.page.evaluate('window.sent'), [])
        self.assertEqual(self.page.locator('input').evaluate_all('(es)=>es.map(e=>e.files.length)'), [0, 0])

    def test_menu_without_file_chooser_reports_exact_step(self):
        self.load(FIXTURE.replace("document.querySelector('input').click()", 'void(0)'))
        with self.assertRaisesRegex(QuizAIError, '文件选择器'):
            self.run_exchange()
        self.assertEqual(self.stages[-1], '等待图片文件选择器')
        self.assertEqual(self.page.evaluate('window.sent'), [])

    def test_cancel_while_waiting_upload_never_sends(self):
        self.load(FIXTURE.replace("document.querySelector('#menu').hidden=false", 'void(0)'))
        original_check = self.control.check
        calls = []
        def check():
            calls.append(1)
            if len(calls) > 4:
                self.control.stopped = True
            original_check()
        self.control.check = check
        with self.assertRaises(KeyboardInterrupt):
            self.run_exchange()
        self.assertEqual(self.page.evaluate('window.sent'), [])
