"""使用独立的无头 Edge 验证实际启动入口，不访问课程或用户浏览器资料。"""
from contextlib import contextmanager, redirect_stdout
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from gui_backend import Bridge, GuiRequest, execute
import main


HTML = '''<!doctype html><style>
html,body{margin:0}main{width:100vw;height:100vh;background:#dff5ff}
</style><main>Local responsive page</main>'''


class BrowserWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()

    @classmethod
    def tearDownClass(cls):
        cls.playwright.stop()

    @contextmanager
    def local_playwright(self):
        def launch(profile, **options):
            # 保留生产入口的尺寸参数，仅改为无头运行并拦截所有测试请求。
            options['headless'] = True
            self.context = self.playwright.chromium.launch_persistent_context(profile, **options)
            self.context.route('**/*', lambda route: route.fulfill(
                content_type='text/html', body=HTML))
            return self.context

        yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

    def measure_windows(self):
        measurements = {}
        course_page = self.context.pages[-1]
        new_tab = self.context.new_page()
        new_tab.goto('https://course.example/new-tab')
        for name, page in [('course', course_page), ('new_tab', new_tab)]:
            page.bring_to_front()
            session = self.context.new_cdp_session(page)
            window_id = session.send('Browser.getWindowForTarget')['windowId']
            samples = []
            for width, height in [(1000, 700), (1600, 1000), (1000, 700)]:
                session.send('Browser.setWindowBounds', {
                    'windowId': window_id, 'bounds': {'width': width, 'height': height}})
                page.wait_for_function(
                    '([w,h]) => outerWidth === w && outerHeight === h', arg=[width, height])
                # 等待布局帧，固定视口的旧实现也能到达下面的行为断言。
                page.evaluate('() => new Promise(resolve => requestAnimationFrame('
                              '() => requestAnimationFrame(resolve)))')
                samples.append(page.evaluate('''() => ({
                    inner: [innerWidth, innerHeight], outer: [outerWidth, outerHeight],
                    content: [document.querySelector('main').getBoundingClientRect().width,
                              document.querySelector('main').getBoundingClientRect().height]
                })'''))
            measurements[name] = samples
            session.detach()
        return measurements

    def assert_responsive(self, measurements):
        self.assertEqual(set(measurements), {'course', 'new_tab'})
        for page_name, (small, large, restored) in measurements.items():
            with self.subTest(page=page_name):
                self.assertAlmostEqual(large['inner'][0] - small['inner'][0], 600, delta=2,
                                       msg=f'网页宽度未跟随窗口：{measurements}')
                self.assertAlmostEqual(large['inner'][1] - small['inner'][1], 300, delta=2,
                                       msg=f'网页高度未跟随窗口：{measurements}')
                self.assertEqual(restored['inner'], small['inner'])
                for sample in (small, large, restored):
                    self.assertEqual(sample['content'], sample['inner'])

    def test_gui_course_and_new_tab_follow_window_resize(self):
        for command in ('run', 'inspect'):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                measurements = {}
                bridge = Bridge()

                def inspect_open_window(*_args):
                    measurements.update(self.measure_windows())
                    return 'cancel'

                request = GuiRequest(command, 'generic', 'resize-test',
                                     url='https://course.example/lesson', background_playback=False)
                with patch('playwright.sync_api.sync_playwright', self.local_playwright), \
                        patch.object(bridge, 'ask', side_effect=inspect_open_window):
                    execute(request, bridge, Path(directory))
                events = []
                while not bridge.events.empty():
                    events.append(bridge.events.get_nowait())
                self.assertEqual(events[-1], {'kind': 'done', 'outcome': 'cancelled'}, events)
                # execute 会捕获异常；行为断言放在它返回后，避免失败被转换为界面提示。
                self.assert_responsive(measurements)

    def test_cli_course_and_new_tab_follow_window_resize(self):
        measurements = {}

        def inspect_open_window(*_args):
            measurements.update(self.measure_windows())
            raise EOFError('Local launch verification completed')

        def close_test_logging(**kwargs):
            for handler in kwargs['handlers']:
                handler.close()

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(main, 'DATA', Path(directory)), \
                patch('playwright.sync_api.sync_playwright', self.local_playwright), \
                patch('main.logging.basicConfig', side_effect=close_test_logging), \
                patch('builtins.input', return_value=''), \
                patch.object(main, 'select_page', side_effect=inspect_open_window), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(EOFError):
                main.main(['inspect', '--platform', 'generic', '--channel', 'msedge',
                           '--url', 'https://course.example/lesson'])
        self.assert_responsive(measurements)


if __name__ == '__main__':
    unittest.main()
