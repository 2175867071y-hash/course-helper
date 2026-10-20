"""U校园 AI 平台入口和本地界面配置回归，不连接真实课程。"""
import gc
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import Store, VideoState, validate_url
from gui import App, PLATFORMS
from gui_backend import GuiRequest, choose_page, resolve_target
from main import START, load_tasks, make_parser, select_page


CONTENT_URL = ('https://ucontent.unipus.cn/_explorationpc_default/pc.html'
               '?cid=1234567890&theme=default#unit-1')


class UaiPlatformTests(unittest.TestCase):
    def test_accepts_only_verified_uai_hosts(self):
        for host in ('ucloud.unipus.cn', 'uai.unipus.cn', 'sso.unipus.cn',
                     'ucontent.unipus.cn'):
            for url in (f'https://{host}/app/course?unit=2#lesson',
                        f'https://{host.upper()}:443/'):
                with self.subTest(url=url):
                    self.assertEqual(validate_url(url, 'uai'), url)

    def test_rejects_lookalike_unverified_and_unsafe_uai_urls(self):
        urls = ('https://unipus.cn/', 'https://u.unipus.cn/',
                'https://child.ucloud.unipus.cn/', 'https://app.unipus.cn/',
                'https://ucloud.unipus.cn.evil.test/', 'https://evilucloud.unipus.cn/',
                'https://ucloud.unipus.cn./', 'http://ucloud.unipus.cn/',
                'https://user:pass@ucloud.unipus.cn/', 'https://ucloud.unipus.cn:444/',
                'https://ucloud.unipus.cn\\@evil.test/', 'https://ucloud.unipus.cn/\nsecret',
                'https://ucontent.unipus.cn.evil.test/', 'https://evilucontent.unipus.cn/',
                'https://child.ucontent.unipus.cn/', 'https://ucontent.unipus.cn./',
                'http://ucontent.unipus.cn/', 'https://user:pass@ucontent.unipus.cn/',
                'https://ucontent.unipus.cn:444/',
                'https://chaoxing.com/', 'https://www.icourse163.org/')
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url, 'uai')

    def test_platforms_cannot_share_unipus_course_urls(self):
        for platform in ('chaoxing', 'mooc', 'unknown'):
            for url in ('https://ucloud.unipus.cn/app/course', CONTENT_URL):
                with self.subTest(platform=platform, url=url), self.assertRaises(ValueError):
                    validate_url(url, platform)
        self.assertEqual(validate_url('https://mooc1.chaoxing.com/course', 'chaoxing'),
                         'https://mooc1.chaoxing.com/course')
        self.assertEqual(validate_url('https://www.icourse163.org/course', 'mooc'),
                         'https://www.icourse163.org/course')

    def test_cli_and_task_file_support_uai(self):
        args = make_parser().parse_args(['run', '--platform', 'uai'])
        self.assertEqual(args.platform, 'uai')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tasks.json'
            for url in ('https://ucloud.unipus.cn/app/course/demo#lesson', CONTENT_URL):
                with self.subTest(url=url):
                    path.write_text(json.dumps({'tasks': [{'id': 'english', 'platform': 'uai',
                        'url': url}]}), encoding='utf-8')
                    task, = load_tasks(path)
                    self.assertEqual(task.platform, 'uai')
                    self.assertEqual(task.max_chapters, 0)
                    self.assertEqual(task.url, url)

    def test_course_content_tab_can_be_selected_in_gui_and_cli(self):
        def page(url, title):
            return SimpleNamespace(url=url, title=lambda: title, is_closed=lambda: False)

        portal = page('https://ucloud.unipus.cn/app/cmgt/resource-detail/123', '课程入口')
        content = page(CONTENT_URL, '英语课件')
        context = SimpleNamespace(pages=[page('about:blank', ''), portal, content,
                                        page('https://ucontent.unipus.cn.evil.test/', '其他')])
        bridge = Mock()
        bridge.ask.return_value = '1'
        self.assertIs(choose_page(context, 'uai', bridge), content)
        self.assertEqual(bridge.ask.call_args.args[2],
                         [('0', '课程入口'), ('1', '英语课件'), ('cancel', '停止')])
        with patch('builtins.input', return_value='2'), patch('builtins.print'):
            self.assertIs(select_page(context, 'uai'), content)

    def test_course_content_url_can_be_opened_and_resumed(self):
        request = GuiRequest('run', 'uai', 'uai:英语课', url=CONTENT_URL)
        self.assertEqual(resolve_target(request, None), CONTENT_URL)
        store = Store(':memory:')
        try:
            store.save(request.task_id, CONTENT_URL,
                       VideoState('video-1', 10, 60, False, False), 'observing')
            request.url = ''
            request.resume = True
            self.assertEqual(resolve_target(request, store), CONTENT_URL)
        finally:
            store.close()

    def test_default_target_has_no_user_course_id(self):
        request = GuiRequest('run', 'uai', 'uai:英语课', resume=False)
        self.assertEqual(START['uai'], 'https://ucloud.unipus.cn/')
        self.assertEqual(resolve_target(request, None), 'https://ucloud.unipus.cn/')


class UaiGuiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.roots = []
        self.apps = []

    def tearDown(self):
        self._destroy_windows()
        self.apps.clear()
        self.roots.clear()
        gc.collect()
        self.directory.cleanup()

    def _destroy_windows(self):
        for app in self.apps:
            for job in app.jobs.values():
                job.bridge.stop()
            for job in app.jobs.values():
                if job.worker is not None:
                    job.worker.join(2)
        for root in reversed(self.roots):
            for callback in root.tk.splitlist(root.tk.call('after', 'info')):
                root.after_cancel(callback)
            root.destroy()

    def app(self, runner=None):
        root = tk.Tk()
        root.withdraw()
        self.roots.append(root)
        kwargs = {'runner': runner} if runner is not None else {}
        app = App(root, Path(self.directory.name), **kwargs)
        self.apps.append(app)
        return app

    def test_platform_persists_and_restores_with_relevant_hint(self):
        app = self.app()
        self.assertEqual(PLATFORMS.get('U校园 AI'), 'uai')
        app.platform.set('U校园 AI')
        self.assertIn('进入具体课件', app.platform_hint.get())
        for capability in ('文字选择', '填空', '词库', '阅读题'):
            self.assertIn(capability, app.platform_hint.get())
        for limitation in ('听力', '口语', '未适配分页', '需手动处理'):
            self.assertIn(limitation, app.platform_hint.get())
        app._save_settings()
        saved = json.loads((Path(self.directory.name) / 'settings.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['platform'], 'U校园 AI')
        restored = self.app()
        self.assertEqual(restored.platform.get(), 'U校园 AI')
        self.assertIn('进入具体课件', restored.platform_hint.get())
        self.assertIn('1.6', restored.root.title())
        restored.platform.set('超星学习通')
        self.assertNotIn('U校园 AI', restored.platform_hint.get())

    def test_uai_start_creates_correct_request(self):
        requests = []

        def runner(request, bridge, directory):
            requests.append(request)
            bridge.emit('done', outcome='inspected')

        app = self.app(runner)
        app.platform.set('U校园 AI')
        app.course_name.set('英语课')
        app.url.set('https://ucloud.unipus.cn/app/course/demo')
        with patch('gui.messagebox.showerror') as error:
            app.start('inspect')
            error.assert_not_called()
        deadline = time.monotonic() + 3
        while app.worker is not None and time.monotonic() < deadline:
            app.root.update()
            time.sleep(0.01)
        self.assertIsNone(app.worker)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].platform, 'uai')
        self.assertEqual(requests[0].task_id, 'uai:英语课')
        self.assertEqual(requests[0].max_chapters, 0)


if __name__ == '__main__':
    unittest.main()
