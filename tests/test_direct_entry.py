"""用离线 Edge 课件验证桌面程序从网址到学习任务的入口。"""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from gui_backend import Bridge, GuiRequest, execute


class RecordingBridge(Bridge):
    def __init__(self):
        super().__init__()
        self.prompts = []

    def ask(self, title, message, choices, **options):
        self.prompts.append((title, message, choices))
        return 'cancel'


class DirectEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel='msedge', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def open_course(self, html, *, redirect=False):
        target = 'https://course.example/lesson/one'
        context = self.browser.new_context()

        def serve(route):
            if redirect and route.request.url == target:
                route.fulfill(status=302, headers={'Location': 'https://unrelated.example/demo'})
            else:
                route.fulfill(content_type='text/html', body=html)
        context.route('**/*', serve)
        # 同一浏览器资料中恢复了别的课程，本次明确提供的网址优先。
        context.new_page().goto('https://course.example/previous')
        launcher = SimpleNamespace(launch_persistent_context=lambda *a, **kw: context)

        class Driver:
            def __enter__(self):
                return SimpleNamespace(chromium=launcher)

            def __exit__(self, *args):
                pass

        started = []
        def observe(adapter, task, store, poll, stall, *, control, activities):
            started.append(task.url)
            return 'q'

        bridge = RecordingBridge()
        request = GuiRequest('run', 'generic', '本地课件', url=target, background_playback=False)
        with TemporaryDirectory() as directory, \
                patch('playwright.sync_api.sync_playwright', return_value=Driver()), \
                patch('gui_backend.run_task', side_effect=observe):
            execute(request, bridge, Path(directory))
        return started, bridge.prompts

    def test_direct_lesson_starts_without_a_ready_or_tab_prompt(self):
        lessons = {
            'video': '<video width="300" height="160"></video>',
            'quiz': '<form><fieldset><legend>题目</legend><label><input type=radio>A</label>'
                    '<label><input type=radio>B</label></fieldset></form>',
            'document': '<div class="document-viewer">第一页</div>',
            'lazy frame': '<iframe srcdoc="<body></body>"></iframe><script>setTimeout(() => {'
                          'document.querySelector(\'iframe\').contentDocument.body.innerHTML='
                          '\'<video width=300 height=160></video>\'}, 450)</script>',
        }
        for name, html in lessons.items():
            with self.subTest(name=name):
                started, prompts = self.open_course(html)
                self.assertEqual(started, ['https://course.example/lesson/one'])
                self.assertEqual(prompts, [])

    def test_login_or_catalog_keeps_manual_entry_available(self):
        for html in ('<input type=password><video></video>',
                     '<main><h1>课程目录</h1><a href="/unit-1">第一章课程</a></main>'):
            with self.subTest(html=html):
                started, prompts = self.open_course(html)
                self.assertEqual(started, [])
                self.assertEqual(len(prompts), 1)
                self.assertIn(('ready', '我已打开课程'), prompts[0][2])
                self.assertIn(('locate', '先定位目录'), prompts[0][2])

    def test_redirect_to_another_generic_site_requires_confirmation(self):
        started, prompts = self.open_course('<video></video>', redirect=True)
        self.assertEqual(started, [])
        self.assertEqual(len(prompts), 1)


if __name__ == '__main__':
    unittest.main()
