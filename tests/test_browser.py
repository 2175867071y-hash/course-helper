import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


class DomItems:
    def __init__(self, items):
        self.items = items

    def all(self):
        return self.items

    def count(self):
        return len(self.items)


class DomVideo:
    def __init__(self):
        self.data = {'source': '', 'id': 'video_html5_api', 'current': 0,
                     'duration': 0, 'paused': True, 'ended': False,
                     'ready': 0, 'error': 0}

    def is_visible(self):
        return True

    def evaluate(self, expression):
        from browser import VIDEO_JS
        if expression != VIDEO_JS:
            raise AssertionError('本测试只允许媒体状态读取和真实播放按钮点击')
        return dict(self.data)


class DomPlayButton:
    def __init__(self):
        self.clicks = 0
        self.fail_click = False

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def click(self, timeout):
        self.clicks += 1
        if self.fail_click:
            raise RuntimeError('点击后网页失去连接')


class DomFrame:
    parent_frame = None
    url = 'https://mooc1.chaoxing.com/ananas/modules/video/index.html'

    def __init__(self):
        self.video = DomVideo()
        self.button = DomPlayButton()

    def locator(self, selector):
        return DomItems([self.video] if selector == 'video' else [])

    def get_by_role(self, role, name):
        return DomItems([self.button] if role == 'button' and name.search('播放视频') else [])


class DomPage:
    def __init__(self):
        self.url = 'https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId=1'
        self.frames = [DomFrame()]

    def set_default_timeout(self, timeout):
        pass

    def on(self, event, callback):
        pass


class StartupContracts(unittest.TestCase):
    """以 DOM 边界替身运行真实适配器，防止同一播放请求被重复发送。"""
    def setUp(self):
        from browser import Adapter, ManualRequired
        self.manual = ManualRequired
        self.page = DomPage()
        self.frame = self.page.frames[0]
        self.adapter = Adapter(self.page, 'chaoxing')

    def test_same_video_is_clicked_only_once(self):
        self.frame.video.data.update(source='https://media.example/video1.mp4',
                                     duration=10, ready=4)
        self.adapter.play(self.adapter.videos()[0])
        with self.assertRaises(self.manual):
            self.adapter.play(self.adapter.videos()[0])
        self.assertEqual(self.frame.button.clicks, 1)

    def test_lazy_source_becoming_available_does_not_allow_second_click(self):
        self.adapter.play(self.adapter.videos()[0])
        self.frame.video.data['source'] = 'https://media.example/video1.mp4'
        self.adapter.play(self.adapter.videos()[0])
        self.frame.video.data.update(duration=10, ready=1)
        self.adapter.play(self.adapter.videos()[0])
        self.frame.video.data['ready'] = 4
        with self.assertRaises(self.manual):
            self.adapter.play(self.adapter.videos()[0])
        self.assertEqual(self.frame.button.clicks, 1)

    def test_new_chapter_can_start_even_while_prior_metadata_was_missing(self):
        self.adapter.play(self.adapter.videos()[0])
        self.page.url = 'https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId=2'
        self.adapter.play(self.adapter.videos()[0])
        self.assertEqual(self.frame.button.clicks, 2)

    def test_click_failure_is_not_retried_automatically(self):
        self.frame.video.data.update(source='https://media.example/video1.mp4',
                                     duration=10, ready=4)
        self.frame.button.fail_click = True
        with self.assertRaises(RuntimeError):
            self.adapter.play(self.adapter.videos()[0])
        self.frame.button.fail_click = False
        with self.assertRaises(self.manual):
            self.adapter.play(self.adapter.videos()[0])
        self.assertEqual(self.frame.button.clicks, 1)


class BrowserContracts(unittest.TestCase):
    def test_uai_course_redirect_is_not_treated_as_leaving_platform(self):
        api = self.api()
        page = DomPage()
        page.url = 'https://ucloud.unipus.cn/app/cmgt/resource-detail/123'
        adapter = api.Adapter(page, 'uai')
        self.assertIsNone(adapter.guard())
        page.url = 'https://ucontent.unipus.cn/_explorationpc_default/pc.html?cid=123'
        self.assertIsNone(adapter.guard())
        self.assertIsNone(adapter.guard(include_course=False))
        page.url = 'https://ucontent.unipus.cn.evil.test/pc.html?cid=123'
        self.assertIn('离开', adapter.guard() or '')

    def test_uai_sso_is_not_treated_as_a_ready_course(self):
        from unittest.mock import patch
        api = self.api()
        page = DomPage()
        page.url = 'https://sso.unipus.cn/sso/login'
        adapter = api.Adapter(page, 'uai')
        with patch('browser.validate_url', return_value=page.url):
            self.assertIn('登录', adapter.guard(include_course=False) or '')

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('browser'), '缺少 browser 实现')
        import browser
        return browser

    def test_page_identity_keeps_chapter_but_drops_rotating_tokens(self):
        api = self.api()
        a = 'https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId=1&courseId=2&enc=a'
        b = 'https://mooc1.chaoxing.com/mycourse/studentstudy?enc=b&courseId=2&chapterId=1'
        self.assertEqual(api.page_identity(a), api.page_identity(b))
        self.assertNotEqual(api.page_identity(a), api.page_identity(a.replace('chapterId=1', 'chapterId=3')))

    def test_mooc_spa_hash_identifies_a_different_lesson(self):
        api = self.api()
        self.assertNotEqual(api.page_identity('https://www.icourse163.org/learn/A#/learn/content?lessonId=1'),
                            api.page_identity('https://www.icourse163.org/learn/A#/learn/content?lessonId=2'))

    def test_ambiguous_video_is_never_chosen_implicitly(self):
        api = self.api()
        with self.assertRaises(api.ManualRequired):
            api.only_one([object(), object()], '视频')
        self.assertIsNone(api.only_one([], '视频'))
        item = object()
        self.assertIs(api.only_one([item], '视频'), item)

    def test_video_next_accepts_document_destination(self):
        from types import SimpleNamespace
        from core import VideoState
        from unittest.mock import Mock, patch
        api = self.api()
        adapter = api.Adapter(DomPage(), 'chaoxing')
        adapter.guard = Mock(return_value=None)
        adapter.task_pending = Mock(return_value=False)
        adapter.activity_identity = Mock(side_effect=['', 'document:new'])
        adapter.videos = Mock(return_value=[SimpleNamespace(state=VideoState('old', 2, 2, True, True))])
        button = Mock()
        adapter.next_control = Mock(return_value=button)
        with patch('browser.time.monotonic', side_effect=[0, 1]):
            adapter.advance('old')
        button.click.assert_called_once()

    def test_document_next_accepts_video_without_second_click(self):
        from types import SimpleNamespace
        from core import VideoState
        from unittest.mock import Mock, patch
        api = self.api()
        adapter = api.Adapter(DomPage(), 'chaoxing')
        adapter.guard = Mock(return_value=None)
        adapter.task_pending = Mock(return_value=False)
        adapter.activity_identity = Mock(side_effect=['document:old', '', ''])
        video = SimpleNamespace(state=VideoState('new', 0, 10, False, False))
        adapter.videos = Mock(side_effect=[[], [video], [video]])
        button = Mock()
        adapter.next_control = Mock(return_value=button)
        with patch('browser.time.monotonic', side_effect=[0, 1]):
            adapter.advance_activity('document:old')
            adapter.advance_activity('document:old')
        button.click.assert_called_once()

    def test_activity_next_timeout_does_not_repeat_click(self):
        from unittest.mock import Mock, patch
        api = self.api()
        adapter = api.Adapter(DomPage(), 'chaoxing')
        adapter.guard = Mock(return_value=None)
        adapter.task_pending = Mock(return_value=False)
        adapter.activity_identity = Mock(return_value='document:old')
        adapter.videos = Mock(return_value=[])
        button = Mock()
        adapter.next_control = Mock(return_value=button)
        with patch('browser.time.monotonic', side_effect=[0, 31]):
            for _ in range(2):
                with self.assertRaises(api.ManualRequired):
                    adapter.advance_activity('document:old')
        button.click.assert_called_once()


class TaskContracts(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('main'), '缺少 main 实现')
        import main
        return main

    def load(self, content):
        api = self.api()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'tasks.json'
            path.write_text(json.dumps(content), encoding='utf-8')
            return api.load_tasks(path)

    def test_load_valid_explicit_queue(self):
        tasks = self.load({'tasks': [{'id':'math', 'platform':'mooc', 'url':'https://www.icourse163.org/learn/course'}]})
        self.assertEqual(tasks[0].task_id, 'math')
        self.assertEqual(tasks[0].platform, 'mooc')

    def test_duplicate_ids_rejected(self):
        task = {'id':'same', 'platform':'chaoxing', 'url':'https://mooc1.chaoxing.com/mycourse/studentstudy'}
        with self.assertRaises(ValueError):
            self.load({'tasks':[task,task]})

    def test_invalid_queue_rejected_before_browser_is_launched(self):
        for content in ({'tasks':[]}, {'tasks':[{'id':'x','platform':'mooc','url':'https://evil.test'}]},
                        {'tasks':[{'id':'x','platform':'typo','url':'https://www.icourse163.org'}]},
                        {'tasks':[{'id':'x','platform':'mooc','url':'https://www.icourse163.org','max_chapters':-1}]}):
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.load(content)


if __name__ == '__main__':
    unittest.main()
