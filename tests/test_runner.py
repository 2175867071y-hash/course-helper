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
