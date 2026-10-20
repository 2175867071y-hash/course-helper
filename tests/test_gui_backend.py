"""界面线程桥接：无需联网或控制真实浏览器。"""
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gui_backend import Bridge, Cancelled, GuiRequest, execute, resolve_target
from core import Store, VideoState


class BridgeTests(unittest.TestCase):
    def test_new_requests_default_to_unlimited_and_hide_api_key(self):
        request = GuiRequest('run', 'chaoxing', 'course')
        self.assertEqual(request.max_chapters, 0)
        request.ai_api_key = 'test-private-key'
        request.speech_api_key = 'test-private-speech-key'
        self.assertNotIn('test-private-key', repr(request))
        self.assertNotIn('test-private-speech-key', repr(request))
        self.assertTrue(request.auto_documents)
        self.assertTrue(request.auto_quiz)
        self.assertTrue(request.quiz_submit)

    def test_finished_session_keeps_browser_until_close_reply(self):
        for outcome in ('limit', 's'):
            with self.subTest(outcome=outcome):
                self.finished_session(outcome)

    def finished_session(self, outcome):
        closed, activity_options, run_options, setup = [], [], [], []
        page = SimpleNamespace(url='https://mooc1.chaoxing.com/mycourse/studentstudy',
                               goto=lambda *args, **kwargs: setup.append('goto'), frames=[],
                               title=lambda: '课程', is_closed=lambda: False)
        context = SimpleNamespace(new_page=lambda: page, pages=[page],
                                  add_init_script=lambda **kwargs: setup.append('background'),
                                  close=lambda: closed.append(True))
        chromium = SimpleNamespace(launch_persistent_context=lambda *args, **kwargs: context)

        class Driver:
            def __enter__(self):
                return SimpleNamespace(chromium=chromium)

            def __exit__(self, *args):
                return False

        class Activities:
            def __init__(self, adapter, **kwargs):
                activity_options.append(kwargs)

        def finished(*args, **kwargs):
            run_options.append(kwargs)
            return outcome

        request = GuiRequest('run', 'chaoxing', 'course', max_chapters=2)
        request.background_playback = outcome == 'limit'
        request.auto_documents = True
        request.auto_quiz = True
        request.quiz_submit = outcome == 'limit'
        request.document_seconds = 12
        request.ai_base_url = 'https://api.example/v1'
        request.ai_model = 'test-model'
        request.ai_api_key = 'test-private-key'
        request.vision_model = 'vision-model'
        request.speech_base_url = 'https://speech.example/v1'
        request.speech_model = 'transcribe-model'
        request.speech_api_key = 'speech-private-key'
        bridge = Bridge()
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict('sys.modules', {'playwright.sync_api': SimpleNamespace(sync_playwright=Driver),
                                       'activities': SimpleNamespace(CourseActivities=Activities)}), \
             patch('gui_backend.Adapter', return_value=SimpleNamespace(page=page,
                   navigator=SimpleNamespace(load=lambda path: None))), \
             patch('gui_backend.run_task', side_effect=finished):
            worker = threading.Thread(target=execute, args=(request, bridge, Path(directory)))
            worker.start()
            try:
                login = self.next_prompt(bridge)
                self.assertTrue(bridge.reply(login['request_id'], 'ready'))
                ended = self.next_prompt(bridge)
                self.assertEqual(closed, [], '学习结束后应保留浏览器供用户核对')
                self.assertEqual(ended['choices'], [('close', '关闭浏览器并返回')])
                self.assertTrue(bridge.reply(ended['request_id'], 'close'))
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(closed, [True])
                self.assertEqual(setup, ['background', 'goto'] if request.background_playback else ['goto'])
                self.assertEqual(activity_options[0]['ai_api_key'], 'test-private-key')
                self.assertEqual(activity_options[0]['document_seconds'], 12)
                self.assertEqual(activity_options[0]['ai_model'], 'test-model')
                self.assertEqual(activity_options[0]['vision_model'], 'vision-model')
                self.assertEqual(activity_options[0]['speech_base_url'], 'https://speech.example/v1')
                self.assertEqual(activity_options[0]['speech_model'], 'transcribe-model')
                self.assertEqual(activity_options[0]['speech_api_key'], 'speech-private-key')
                self.assertEqual(activity_options[0]['quiz_submit'], outcome == 'limit')
                self.assertIsInstance(run_options[0]['activities'], Activities)
            finally:
                bridge.stop()
                worker.join(timeout=2)

    def next_prompt(self, bridge):
        while True:
            event = bridge.events.get(timeout=2)
            if event['kind'] == 'prompt':
                return event
            if event['kind'] == 'done':
                self.fail(f'应等待用户操作，但任务已结束：{event}')

    def test_invalid_activity_parameters_are_rejected_before_browser(self):
        invalid = [('max_chapters', -1), ('max_chapters', True),
                   ('document_seconds', 2), ('document_seconds', 121),
                   ('auto_documents', 'yes'), ('auto_quiz', 1),
                   ('quiz_submit', 1), ('quiz_submit', 'false'), ('quiz_submit', None),
                   ('ai_api_key', None)]
        for name, value in invalid:
            with self.subTest(name=name, value=value), tempfile.TemporaryDirectory() as directory:
                request = GuiRequest('run', 'chaoxing', 'course', max_chapters=2)
                setattr(request, name, value)
                bridge = Bridge()
                with patch('gui_backend.Store', side_effect=AssertionError(
                        '无效配置不能创建运行资源')) as create_store:
                    execute(request, bridge, Path(directory))
                create_store.assert_not_called()
                events = []
                while not bridge.events.empty():
                    events.append(bridge.events.get_nowait())
                self.assertEqual(events[-1]['outcome'], 'error')
                self.assertFalse(Path(directory, 'progress.sqlite3').exists(),
                                 '无效配置必须在创建运行资源之前被拒绝')

    def test_stop_releases_pending_question(self):
        bridge = Bridge(queue.Queue())
        result = []

        def ask():
            try:
                bridge.ask('登录', '请打开课程', [('ready', '已打开')])
            except Cancelled:
                result.append('cancelled')

        worker = threading.Thread(target=ask)
        worker.start()
        event = bridge.events.get(timeout=1)
        self.assertEqual(event['kind'], 'prompt')
        bridge.stop()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, ['cancelled'])

    def test_replies_must_match_request_and_choice(self):
        bridge = Bridge(queue.Queue())
        answers = []
        worker = threading.Thread(target=lambda: answers.append(
            bridge.ask('选择', '请选择课程', [('a', '课程 A')])) )
        worker.start()
        event = bridge.events.get(timeout=1)
        self.assertFalse(bridge.reply(event['request_id'] + 1, 'a'))
        self.assertFalse(bridge.reply(event['request_id'], 'unknown'))
        self.assertTrue(bridge.reply(event['request_id'], 'a'))
        worker.join(timeout=1)
        self.assertEqual(answers, ['a'])
        self.assertFalse(bridge.reply(event['request_id'], 'a'))

    def test_wait_is_cancelled(self):
        bridge = Bridge(queue.Queue())
        bridge.stop()
        with self.assertRaises(Cancelled):
            bridge.sleep(30)

    def test_resume_url_validated_for_selected_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'progress.sqlite3')
            try:
                store.save('course', 'https://evil.example/course',
                           VideoState('v', 1, 10, False, False), 'observing')
                request = GuiRequest('run', 'chaoxing', 'course', resume=True)
                with self.assertRaises(ValueError):
                    resolve_target(request, store)
            finally:
                store.close()

    def test_cancel_before_start_does_not_launch_browser(self):
        bridge = Bridge(queue.Queue())
        bridge.stop()
        with tempfile.TemporaryDirectory() as directory:
            execute(GuiRequest('run', 'chaoxing', 'course'), bridge, Path(directory))
        events = []
        while not bridge.events.empty():
            events.append(bridge.events.get_nowait())
        self.assertEqual(events[-1], {'kind': 'done', 'outcome': 'cancelled'})

    def test_cancel_at_login_closes_browser_and_releases_database(self):
        closed = []
        page = SimpleNamespace(goto=lambda *args, **kwargs: None, frames=[])
        context = SimpleNamespace(new_page=lambda: page, close=lambda: closed.append(True),
                                  pages=[page], add_init_script=lambda **kwargs: None)
        chromium = SimpleNamespace(launch_persistent_context=lambda *args, **kwargs: context)

        class Driver:
            def __enter__(self):
                return SimpleNamespace(chromium=chromium)

            def __exit__(self, *args):
                return False

        bridge = Bridge(queue.Queue())
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict('sys.modules', {'playwright.sync_api': SimpleNamespace(sync_playwright=Driver)}):
                worker = threading.Thread(target=execute, args=(
                    GuiRequest('run', 'chaoxing', 'course'), bridge, Path(directory)))
                worker.start()
                try:
                    while bridge.events.get(timeout=2)['kind'] != 'prompt':
                        pass
                finally:
                    bridge.stop()
                    worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(closed, [True])
                self.assertEqual(bridge.events.get(timeout=1)['outcome'], 'cancelled')
            store = Store(Path(directory) / 'progress.sqlite3')
            self.assertEqual(store.rows(), [])
            store.close()


if __name__ == '__main__':
    unittest.main()
