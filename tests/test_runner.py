import itertools
import inspect
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from browser import Adapter, ManualRequired
from core import Store, VideoState
from main import Task, load_tasks, make_parser, run_task


URL1 = 'https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId=1'
URL2 = 'https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId=2'


class CoursePage:
    def __init__(self):
        self.url = URL1

    def is_closed(self):
        return False

    def set_default_timeout(self, value):
        pass

    def on(self, event, callback):
        pass


class CourseAdapter:
    """仅替代外部网页 I/O；使用真实 Monitor、Store、run_task。"""
    def __init__(self, move_before_error=False):
        self.page = CoursePage()
        self.reads = 0
        self.advances = 0
        self.move_before_error = move_before_error

    def guard(self):
        return None

    def videos(self):
        if self.advances >= 2:
            raise KeyboardInterrupt
        current = min(self.reads, 2)
        self.reads += 1
        return [SimpleNamespace(state=VideoState('video1', current, 2,
                                                current == 2, current == 2))]

    def task_pending(self):
        return False

    def advance(self, key):
        self.advances += 1
        if self.move_before_error:
            self.page.url = URL2
        if self.advances == 1:
            raise ManualRequired('课程页面暂时不可切节')

    def pause(self):
        pass


class RunnerTests(unittest.TestCase):
    def test_quiz_pause_context_does_not_leak_to_next_video(self):
        from activities import Activity, CourseActivities
        from gui_backend import Bridge

        class PausedVideo(CourseAdapter):
            def guard(self, include_course=True):
                return None

            def videos(self):
                return [SimpleNamespace(state=VideoState('new-video', 1, 800, True, False))]

            def play(self, video):
                raise ManualRequired('请点击视频播放')

        prompts = []

        class AnsweringBridge(Bridge):
            def emit(self, kind, **payload):
                super().emit(kind, **payload)
                if kind == 'prompt':
                    prompts.append(payload)
                    self.reply(payload['request_id'], 'r' if len(prompts) == 1 else 'q')

        adapter = PausedVideo()
        bridge = AnsweringBridge()
        activities = CourseActivities(adapter, auto_quiz=False, control=bridge)
        quiz = Activity('quiz', 'q1', None, None)
        with StoreFixture() as store, \
             patch.object(activities, '_detect', side_effect=[quiz, None]):
            result = run_task(adapter, Task('context', 'chaoxing', URL1), store,
                              0, 90, control=bridge, activities=activities)
        self.assertEqual(result, 'q')
        self.assertEqual(len(prompts), 2)
        self.assertEqual(prompts[0].get('context'), 'quiz')
        self.assertEqual(prompts[1].get('context', ''), '')
        self.assertIn('视频', prompts[1]['message'])

    def test_zero_limit_allows_two_consecutive_videos(self):
        adapter = MixedCourse(['video1', 'video2'])
        result, store = self.run_mixed(adapter)
        self.assertEqual(result, 's')
        self.assertEqual(adapter.video_advances, ['video1', 'video2'])
        self.assertEqual({row['video_key'] for row in store.rows()}, {'video1', 'video2'})

    def test_video_document_quiz_video_keeps_independent_progress(self):
        self.assertIn('activities', inspect.signature(run_task).parameters)
        adapter = MixedCourse(['video1', 'document', 'quiz', 'video2'])
        activities = MixedActivities(adapter)
        result, store = self.run_mixed(adapter, activities)
        self.assertEqual(result, 's')
        self.assertEqual(adapter.video_advances, ['video1', 'video2'])
        self.assertEqual(adapter.activity_advances, ['document', 'quiz'])
        self.assertEqual(activities.processed, ['document', 'quiz'])
        self.assertEqual(len(store.rows()), 2, '不能把文档或小测伪记为视频完成')
        self.assertTrue(all(row['ended'] for row in store.rows()))

    def test_platform_auto_document_is_not_skipped_and_video_count_is_exact(self):
        class AutoDocumentCourse(MixedCourse):
            def task_pending(self):
                if self.kind == 'video1':
                    self.move()
                return False

        adapter = AutoDocumentCourse(['video1', 'document', 'quiz', 'video2'])
        activities = MixedActivities(adapter)
        events = []
        control = SimpleNamespace(check=lambda: None, sleep=lambda seconds: None,
                                  emit=lambda kind, **data: events.append((kind, data)))
        clock = itertools.count()
        with StoreFixture() as store, patch('main.time.monotonic', side_effect=lambda: next(clock)):
            result = run_task(adapter, Task('all', 'chaoxing', URL1, 2), store,
                              1, 90, control=control, activities=activities)
        self.assertEqual(result, 'limit')
        self.assertEqual(adapter.video_advances, [], '自动切页及达到上限时不额外点下一节')
        self.assertEqual(adapter.activity_advances, ['document', 'quiz'])
        finished = [data['completed'] for kind, data in events if kind == 'progress']
        self.assertEqual(max(finished), 2)
        self.assertEqual(finished[-1], 2)

    def test_activity_error_does_not_overwrite_previous_video_status(self):
        adapter = MixedCourse(['video1', 'document'])

        class BlockedDocument(MixedActivities):
            def process(self):
                if self.identity():
                    raise ManualRequired('文档需要人工处理')
                return None

        result, store = self.run_mixed(adapter, BlockedDocument(adapter))
        self.assertEqual(result, 's')
        self.assertEqual(store.rows()[0]['status'], 'video_ended')

    def test_activity_wait_stops_promptly(self):
        from gui_backend import Bridge
        self.assertIn('activities', inspect.signature(run_task).parameters)
        adapter = MixedCourse(['document'])
        bridge = Bridge()

        class WaitingActivity:
            def process(self):
                bridge.stop()
                return 'working'

        with StoreFixture() as store:
            result = run_task(adapter, Task('stop', 'chaoxing', URL1, 0),
                              store, 100, 90, control=bridge, activities=WaitingActivity())
        self.assertEqual(result, 'q')
        self.assertTrue(adapter.paused)
        self.assertEqual(adapter.activity_advances, [])

    def test_queue_and_parser_accept_zero_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tasks.json'
            path.write_text(json.dumps({'tasks': [{'id': 'all', 'platform': 'chaoxing',
                                                   'url': URL1, 'max_chapters': 0}]}), encoding='utf-8')
            tasks = load_tasks(path)
        self.assertEqual(tasks[0].max_chapters, 0)
        self.assertEqual(make_parser().parse_args(['run', '--max-chapters', '0']).max_chapters, 0)

    def run_mixed(self, adapter, activities=None):
        store = Store(':memory:')
        self.addCleanup(store.close)
        clock = itertools.count()
        kwargs = {'activities': activities} if activities is not None else {}
        with patch('main.time.monotonic', side_effect=lambda: next(clock)), \
             patch('main.time.sleep'), patch('main.ask_manual', return_value='s'):
            result = run_task(adapter, Task('whole', 'chaoxing', URL1, 0),
                              store, 1, 90, **kwargs)
        return result, store

    def test_lazy_video_is_started_then_observed_to_natural_end(self):
        class LazyCourse(CourseAdapter):
            def __init__(self):
                super().__init__()
                self.starts = 0

            def play(self, video):
                self.starts += 1

            def videos(self):
                if not self.starts:
                    state = VideoState('lazy', 0, 0, True, False, ready=0)
                else:
                    samples = [VideoState('lazy', 0, 0, True, False, ready=0),
                               VideoState('lazy', 0, 2, True, False, ready=1),
                               VideoState('lazy', 0, 2, False, False),
                               VideoState('lazy', 1, 2, False, False),
                               VideoState('lazy', 2, 2, True, True)]
                    state = samples[min(self.reads, len(samples) - 1)]
                    self.reads += 1
                return [SimpleNamespace(state=state)]

        adapter = LazyCourse()
        store = Store(':memory:')
        self.addCleanup(store.close)
        clock = itertools.count()
        with patch('main.time.monotonic', side_effect=lambda: next(clock)), \
             patch('main.time.sleep'), patch('main.ask_manual', return_value='s'):
            result = run_task(adapter, Task('lazy-test', 'chaoxing', URL1, 1), store, 1, 90)
        self.assertEqual(result, 'limit')
        self.assertEqual(adapter.starts, 1)
        self.assertEqual(store.rows()[0]['status'], 'video_ended')

    def run_fixture(self, adapter, replies):
        store = Store(':memory:')
        self.addCleanup(store.close)
        clock = itertools.count()
        with patch('main.time.monotonic', side_effect=lambda: next(clock)), \
             patch('main.time.sleep'), patch('main.ask_manual', side_effect=replies):
            result = run_task(adapter, Task('test', 'chaoxing', URL1, 10), store, 1, 90)
        return result, store

    def test_retry_keeps_observed_end_when_navigation_failed_before_click(self):
        adapter = CourseAdapter()
        self.run_fixture(adapter, ['r', 'q'])
        self.assertEqual(adapter.advances, 2,
                         '已自然结束的视频在人工恢复后应继续尝试导航')

    def test_navigation_error_never_associates_old_video_with_new_url(self):
        _, store = self.run_fixture(CourseAdapter(move_before_error=True), ['s'])
        rows = store.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['url'], URL1)
        self.assertEqual(rows[0]['video_key'], 'video1')

    def test_timed_out_next_click_is_never_repeated_for_same_video(self):
        adapter = Adapter(CoursePage(), 'chaoxing')
        clicks = []
        button = SimpleNamespace(click=lambda **kwargs: clicks.append(1))
        adapter.guard = lambda: None
        adapter.task_pending = lambda: False
        adapter.next_control = lambda: button
        adapter.videos = lambda: [SimpleNamespace(state=VideoState('same-video', 2, 2, True, True))]
        with patch('browser.time.monotonic', side_effect=[0, 31, 0, 31]):
            for _ in range(2):
                with self.assertRaises(ManualRequired):
                    adapter.advance('same-video')
        self.assertEqual(len(clicks), 1)

    def test_platform_auto_advance_does_not_skip_the_new_video(self):
        adapter = Adapter(CoursePage(), 'chaoxing')
        clicks = []
        adapter.guard = lambda: None
        adapter.task_pending = lambda: False
        adapter.next_control = lambda: SimpleNamespace(click=lambda **kwargs: clicks.append(1))
        adapter.videos = lambda: [SimpleNamespace(state=VideoState('new-video', 0, 20, False, False))]
        with patch('browser.time.monotonic', side_effect=[0, 31]):
            try:
                adapter.advance('old-video')
            except ManualRequired:
                pass
        self.assertEqual(clicks, [], '平台已经切节时不能再点击而跳过视频')

    def test_advance_activity_result_page_new_key_still_advances(self):
        # 交卷后题目指纹变化会产生新的 key；只要新 key 已完成（同一份试卷的结果页），
        # 就继续切下一节，而不是误判成“页面已自动切换”。
        adapter = Adapter(CoursePage(), 'chaoxing')
        clicks = []
        def click(**kwargs):
            clicks.append(1)
            adapter.page.url = URL2
        adapter.guard = lambda include_course=True: None
        adapter.task_pending = lambda: False
        adapter.next_control = lambda: SimpleNamespace(click=click)
        adapter.videos = lambda: []
        adapter.switch_activity_tab = lambda old_key, timeout=30: None
        adapter._task_pending_settled = lambda seconds=10: False
        adapter.activity_identity = lambda: 'new-quiz-key'
        adapter.activity_completed = lambda key: key == 'new-quiz-key'
        adapter.advance_activity('old-quiz-key', timeout=1)
        self.assertEqual(clicks, [1], '结果页的新 key 不应阻止切下一节')
        self.assertEqual(adapter.page.url, URL2)

    def test_retry_after_stalled_activity_tab_never_leaves_the_section(self):
        for source in ('video', 'document'):
            for tab_response in (False, True):
                with self.subTest(source=source, tab_response=tab_response):
                    adapter = Adapter(CoursePage(), 'chaoxing')
                    chapter_clicks, tab_clicks = [], []
                    adapter.guard = lambda **kwargs: None
                    adapter.task_pending = lambda: False
                    adapter.next_control = lambda: SimpleNamespace(click=lambda **kw: chapter_clicks.append(1))
                    adapter.switch_activity_tab = lambda *args: tab_clicks.append(1) or tab_response
                    if source == 'video':
                        adapter.activity_identity = lambda: ''
                        adapter.videos = lambda: [SimpleNamespace(state=VideoState(source, 5, 5, True, True))]
                        advance = adapter.advance
                    else:
                        adapter.activity_identity = lambda: source
                        adapter.videos = lambda: []
                        advance = adapter.advance_activity
                    for _ in range(2):
                        try:
                            advance(source, timeout=0)
                        except ManualRequired:
                            pass
                    self.assertEqual(tab_clicks, [1])
                    self.assertEqual(chapter_clicks, [], '仍显示上一活动时不能跳过本节小测')

    def test_advance_activity_unfinished_new_key_returns_without_click(self):
        adapter = Adapter(CoursePage(), 'chaoxing')
        clicks = []
        adapter.guard = lambda include_course=True: None
        adapter.task_pending = lambda: False
        adapter.next_control = lambda: SimpleNamespace(click=lambda **kwargs: clicks.append(1))
        adapter.videos = lambda: []
        adapter.activity_identity = lambda: 'brand-new-activity'
        adapter.activity_completed = lambda key: False
        adapter.advance_activity('old-quiz-key', timeout=1)
        self.assertEqual(clicks, [], '未完成的新活动才表示页面已自动切换，不应再点下一节')

    def test_loading_errors_retry_within_window_without_manual(self):
        # 交卷/切节后页面跳转时读取超时，应在窗口内自动重试，而不是打断。
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        adapter = CourseAdapter()
        calls = []
        def videos():
            calls.append(1)
            if len(calls) <= 3:
                raise PlaywrightTimeout('execution context was destroyed')
            current = min(len(calls) - 4, 2)
            return [SimpleNamespace(state=VideoState('v', current, 2,
                                                     current == 2, current == 2))]
        adapter.videos = videos
        store = Store(':memory:')
        self.addCleanup(store.close)
        clock = itertools.count()
        with patch('main.time.monotonic', side_effect=lambda: next(clock)), \
             patch('main.time.sleep'), patch('main.ask_manual', return_value='q') as manual:
            result = run_task(adapter, Task('loading', 'chaoxing', URL1, 1), store, 1, 90)
        self.assertEqual(result, 'limit', '短暂加载超时应自动重试后继续')
        manual.assert_not_called()

    def test_loading_errors_beyond_window_prompt_manual(self):
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        adapter = CourseAdapter()
        def videos():
            raise PlaywrightTimeout('execution context was destroyed')
        adapter.videos = videos
        store = Store(':memory:')
        self.addCleanup(store.close)
        clock = itertools.count()
        with patch('main.time.monotonic', side_effect=lambda: next(clock)), \
             patch('main.time.sleep'), patch('main.ask_manual', return_value='q') as manual:
            result = run_task(adapter, Task('loading', 'chaoxing', URL1, 1), store, 1, 90)
        self.assertEqual(result, 'q', '持续加载超时最终仍需人工处理')
        manual.assert_called_once()


class StoreFixture:
    def __enter__(self):
        self.store = Store(':memory:')
        return self.store

    def __exit__(self, *args):
        self.store.close()


class MixedCourse:
    """只替代页面 I/O，覆盖真实 runner 的视频和非视频连续调度。"""
    def __init__(self, sequence):
        self.sequence = sequence
        self.position = 0
        self.reads = 0
        self.page = CoursePage()
        self.video_advances = []
        self.activity_advances = []
        self.paused = False

    @property
    def kind(self):
        return self.sequence[self.position]

    def guard(self, include_course=True):
        if include_course and self.kind == 'quiz':
            return '课程小测需要处理'
        return None

    def videos(self):
        if not self.kind.startswith('video'):
            return []
        current = min(self.reads, 2)
        self.reads += 1
        return [SimpleNamespace(state=VideoState(self.kind, current, 2,
                                                current == 2, current == 2))]

    def task_pending(self):
        return False

    def move(self):
        if self.position + 1 >= len(self.sequence):
            raise ManualRequired('没有下一节，请确认课程是否结束')
        self.position += 1
        self.reads = 0
        self.page.url = f'https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId={self.position + 1}'

    def advance(self, key):
        self.video_advances.append(key)
        self.move()

    def advance_activity(self, identity):
        self.activity_advances.append(identity)
        self.move()

    def pause(self):
        self.paused = True


class MixedActivities:
    def __init__(self, adapter):
        self.adapter = adapter
        self.waited = set()
        self.processed = []

    def identity(self):
        return self.adapter.kind if not self.adapter.kind.startswith('video') else ''

    def process(self):
        identity = self.identity()
        if not identity:
            return None
        if identity not in self.waited:
            self.waited.add(identity)
            return 'working'
        self.processed.append(identity)
        return 'done'


if __name__ == '__main__':
    unittest.main()
