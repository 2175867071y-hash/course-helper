"""多任务后台的线程、浏览器配置及日志隔离，不访问真实网站。"""
from contextlib import contextmanager
import logging
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gui_backend import Bridge, GuiRequest, _TaskLogging, execute
from core import Store, VideoState


class ReadyBridge(Bridge):
    def ask(self, title, message, choices):
        self.check()
        return choices[0][0]


def drain(bridge):
    events = []
    while not bridge.events.empty():
        events.append(bridge.events.get_nowait())
    return events


@contextmanager
def fake_browser(launches, closed, run):
    def launch(profile, **kwargs):
        owner = threading.current_thread().name
        launches.append((owner, Path(profile)))
        page = SimpleNamespace(url='', title=lambda: '课程', is_closed=lambda: False, frames=[])
        page.goto = lambda url, **kwargs: setattr(page, 'url', url)
        return SimpleNamespace(new_page=lambda: page, pages=[page],
                               add_init_script=lambda **kwargs: None,
                               close=lambda: closed.append(owner))

    class Driver:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(
                launch_persistent_context=launch))

        def __exit__(self, *args):
            return False

    with patch.dict('sys.modules', {
        'playwright.sync_api': SimpleNamespace(sync_playwright=Driver),
        'activities': SimpleNamespace(CourseActivities=lambda *args, **kwargs: object()),
    }), patch('gui_backend.Adapter', side_effect=lambda page, *args, **kwargs:
              SimpleNamespace(page=page, inspect=lambda: {'url': page.url},
                              navigator=SimpleNamespace(load=lambda path: None))), \
            patch('gui_backend.run_task', side_effect=run):
        yield


class ParallelBackendTests(unittest.TestCase):
    def test_closing_one_task_during_dispatch_keeps_other_task_log(self):
        reached, resume = threading.Event(), threading.Event()
        root = logging.getLogger()
        original_level, original_handlers = root.level, list(root.handlers)

        class DispatchGate(logging.Handler):
            def emit(self, record):
                if record.getMessage() == 'surviving-task-log':
                    reached.set()
                    if not resume.wait(2):
                        raise RuntimeError('等待日志交错超时')

        gate = DispatchGate()
        second_bridge = Bridge()
        errors = []
        with tempfile.TemporaryDirectory() as directory:
            first = _TaskLogging(Bridge(), Path(directory) / 'first.log')
            # 将正在遍历的处理器置于首个任务之后，固定触发原地删除列表的竞态。
            root.addHandler(gate)

            def write_log():
                second = _TaskLogging(second_bridge, Path(directory) / 'second.log')
                try:
                    logging.info('surviving-task-log')
                except Exception as exc:
                    errors.append(exc)
                finally:
                    second.close()

            worker = threading.Thread(target=write_log)
            worker.start()
            try:
                self.assertTrue(reached.wait(2))
                first.close()
                resume.set()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertIn('surviving-task-log',
                              (Path(directory) / 'second.log').read_text(encoding='utf-8'))
                self.assertEqual(len(drain(second_bridge)), 1)
            finally:
                resume.set()
                worker.join(2)
                first.close()
                root.removeHandler(gate)
                gate.close()
                root.setLevel(original_level)
        self.assertEqual(root.handlers, original_handlers)

    def test_log_close_error_reports_done_and_does_not_leak_into_next_task(self):
        root = logging.getLogger()
        original_level, original_handlers = root.level, list(root.handlers)
        original_users = _TaskLogging._users
        original_previous_level = _TaskLogging._previous_level
        original_close = logging.FileHandler.close
        first_bridge = ReadyBridge()

        def failed_close(handler):
            original_close(handler)
            raise OSError('模拟日志刷新失败')

        with tempfile.TemporaryDirectory() as directory, fake_browser(
                [], [], lambda *args, **kwargs: 'success'):
            try:
                escaped = None
                try:
                    with patch.object(logging.FileHandler, 'close', failed_close):
                        execute(GuiRequest('run', 'chaoxing', 'course'), first_bridge, directory)
                except Exception as exc:
                    escaped = exc
                self.assertIsNone(escaped, '日志关闭异常必须转换为 GUI done 事件')
                self.assertEqual(drain(first_bridge)[-1]['outcome'], 'error')
                self.assertEqual(root.handlers, original_handlers)
                self.assertEqual(root.level, original_level)
                self.assertEqual(_TaskLogging._users, original_users)

                def next_run(*args, **kwargs):
                    logging.info('next-task-log')
                    return 'success'

                retry = ReadyBridge()
                with patch('gui_backend.run_task', side_effect=next_run):
                    execute(GuiRequest('run', 'chaoxing', 'course'), retry, directory)
                retry_events = drain(retry)
                self.assertEqual(retry_events[-1]['outcome'], 'success')
                self.assertTrue(any('next-task-log' in event.get('message', '')
                                    for event in retry_events))
                self.assertEqual(drain(first_bridge), [])
                self.assertEqual(root.handlers, original_handlers)
                self.assertEqual(root.level, original_level)
            finally:
                # 回归失败时也不把遗留的全局日志状态带入其他测试。
                for handler in list(root.handlers):
                    if handler not in original_handlers:
                        root.removeHandler(handler)
                        handler.close()
                root.setLevel(original_level)
                _TaskLogging._users = original_users
                _TaskLogging._previous_level = original_previous_level

    def test_profiles_are_stable_per_course_and_channel_and_inspection_is_local(self):
        launches, closed = [], []
        requests = [
            GuiRequest('run', 'chaoxing', '秘密课程甲'),
            GuiRequest('run', 'chaoxing', '秘密课程甲'),
            GuiRequest('run', 'chaoxing', '秘密课程乙'),
            GuiRequest('run', 'mooc', '秘密课程甲'),
            GuiRequest('run', 'chaoxing', '秘密课程甲', channel='chrome'),
            GuiRequest('inspect', 'chaoxing', '秘密课程甲'),
        ]
        with tempfile.TemporaryDirectory() as directory, fake_browser(
                launches, closed, lambda *args, **kwargs: 'success'):
            for request in requests:
                bridge = ReadyBridge()
                execute(request, bridge, directory)
                self.assertNotEqual(drain(bridge)[-1]['outcome'], 'error')
            paths = [path for _, path in launches]
            self.assertEqual(paths[0], paths[1])
            self.assertEqual(paths[0], paths[5])
            self.assertEqual(len(set(paths[:5])), 4)
            self.assertTrue(all('秘密课程' not in str(path) for path in paths))
            reports = list(Path(directory).rglob('inspect.json'))
            self.assertEqual(len(reports), 1)
            self.assertNotEqual(reports[0].parent, Path(directory))
            self.assertTrue(Path(directory, 'progress.sqlite3').exists())

    def test_parallel_stop_and_log_cleanup_do_not_affect_other_task(self):
        launches, closed = [], []
        entered = {name: threading.Event() for name in ('alpha', 'beta')}
        release_beta = threading.Event()
        bridges = {name: ReadyBridge() for name in entered}
        root = logging.getLogger()
        original_level = root.level
        original_handlers = list(root.handlers)
        root.setLevel(logging.WARNING)

        def run(adapter, task, store, *args, control, **kwargs):
            logging.info('owned-' + task.task_id)
            entered[task.task_id].set()
            if task.task_id == 'alpha':
                control.sleep(30)
            else:
                while not release_beta.wait(0.01):
                    control.check()
                logging.info('last-beta')
            return 'success'

        workers = []
        directory_context = tempfile.TemporaryDirectory()
        directory = directory_context.name
        try:
            with fake_browser(launches, closed, run):
                for name in entered:
                    worker = threading.Thread(name=name, target=execute, args=(
                        GuiRequest('run', 'chaoxing', name), bridges[name], directory))
                    workers.append(worker)
                    worker.start()
                    self.assertTrue(entered[name].wait(2))
                logging.info('unrelated-main-thread')
                bridges['alpha'].stop()
                workers[0].join(2)
                self.assertFalse(workers[0].is_alive())
                self.assertTrue(workers[1].is_alive())
                self.assertEqual(root.level, logging.INFO)
                self.assertEqual(closed, ['alpha'])
                release_beta.set()
                workers[1].join(2)
                self.assertFalse(workers[1].is_alive())
                self.assertEqual(root.level, logging.WARNING)
                self.assertEqual(root.handlers, original_handlers)
                events = {name: drain(bridge) for name, bridge in bridges.items()}
                self.assertEqual(events['alpha'][-1]['outcome'], 'cancelled')
                self.assertEqual(events['beta'][-1]['outcome'], 'success')
                for name, task_events in events.items():
                    logs = '\n'.join(event['message'] for event in task_events
                                     if event['kind'] == 'log')
                    self.assertIn('owned-' + name, logs)
                    self.assertNotIn('owned-' + ('beta' if name == 'alpha' else 'alpha'), logs)
                    self.assertNotIn('unrelated-main-thread', logs)
                files = list(Path(directory).rglob('helper.log'))
                self.assertEqual(len(files), 2)
                texts = [path.read_text(encoding='utf-8') for path in files]
                self.assertEqual(sum('owned-alpha' in content for content in texts), 1)
                self.assertEqual(sum('owned-beta' in content for content in texts), 1)
                self.assertFalse(any('owned-alpha' in content and 'owned-beta' in content
                                     for content in texts))
                self.assertTrue(any('last-beta' in content for content in texts))
        finally:
            release_beta.set()
            for bridge in bridges.values():
                bridge.stop()
            for worker in workers:
                worker.join(2)
            root.setLevel(original_level)
            directory_context.cleanup()

    def test_duplicate_course_is_rejected_and_can_restart_after_cancel(self):
        launches, closed = [], []
        entered = threading.Event()

        def run(adapter, task, store, *args, control, **kwargs):
            if threading.current_thread().name == 'first':
                entered.set()
                control.sleep(30)
            return 'success'

        first_bridge = ReadyBridge()
        with tempfile.TemporaryDirectory() as directory, fake_browser(launches, closed, run):
            first = threading.Thread(name='first', target=execute, args=(
                GuiRequest('run', 'chaoxing', 'course'), first_bridge, directory))
            first.start()
            try:
                self.assertTrue(entered.wait(2))
                duplicate_bridge = ReadyBridge()
                execute(GuiRequest('run', 'chaoxing', 'course', channel='chrome'),
                        duplicate_bridge, directory)
                result = drain(duplicate_bridge)[-1]
                self.assertEqual(result['outcome'], 'error')
                self.assertIn('已在运行', result['message'])
                self.assertEqual(len(launches), 1)
            finally:
                first_bridge.stop()
                first.join(2)
            self.assertFalse(first.is_alive())
            restarted = ReadyBridge()
            execute(GuiRequest('run', 'chaoxing', 'course'), restarted, directory)
            self.assertEqual(drain(restarted)[-1]['outcome'], 'success')
            self.assertEqual(len(launches), 2)

    def test_failed_task_releases_lease_and_keeps_original_logging_level(self):
        launches, closed = [], []
        root = logging.getLogger()
        previous_level, previous_handlers = root.level, list(root.handlers)
        with tempfile.TemporaryDirectory() as directory, fake_browser(
                launches, closed, lambda *args, **kwargs: 'success'):
            bridge = ReadyBridge()
            with patch('gui_backend.run_task', side_effect=RuntimeError('模拟课程错误')):
                execute(GuiRequest('run', 'chaoxing', 'course'), bridge, directory)
            self.assertEqual(drain(bridge)[-1]['outcome'], 'error')
            self.assertEqual(root.level, previous_level)
            self.assertEqual(root.handlers, previous_handlers)
            retry = ReadyBridge()
            execute(GuiRequest('run', 'chaoxing', 'course'), retry, directory)
            self.assertEqual(drain(retry)[-1]['outcome'], 'success')
            self.assertEqual(len(launches), 2)
            self.assertEqual(len(closed), 2)

    def test_parallel_progress_writes_use_one_database_without_lost_courses(self):
        launches, closed = [], []
        start = threading.Barrier(2)
        bridges = [ReadyBridge(), ReadyBridge()]

        def run(adapter, task, store, *args, **kwargs):
            start.wait(timeout=2)
            for second in range(25):
                store.save(task.task_id, adapter.page.url,
                           VideoState('video', second, 100, False, False), 'observing')
            return 'success'

        with tempfile.TemporaryDirectory() as directory, fake_browser(launches, closed, run):
            workers = [threading.Thread(target=execute, args=(
                GuiRequest('run', 'chaoxing', f'course-{i}'), bridge, directory))
                for i, bridge in enumerate(bridges)]
            for worker in workers:
                worker.start()
            try:
                for worker in workers:
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                self.assertEqual([drain(bridge)[-1]['outcome'] for bridge in bridges],
                                 ['success', 'success'])
                store = Store(Path(directory) / 'progress.sqlite3')
                try:
                    rows = store.rows()
                    self.assertEqual({row['task_id'] for row in rows},
                                     {'course-0', 'course-1'})
                    self.assertEqual([row['current'] for row in rows], [24, 24])
                finally:
                    store.close()
            finally:
                for bridge in bridges:
                    bridge.stop()
                for worker in workers:
                    worker.join(2)

    def test_cleanup_error_still_reports_done_and_releases_task(self):
        launches, closed = [], []
        real_close = Store.close

        def failed_close(store):
            real_close(store)
            raise RuntimeError('模拟记录关闭异常')

        with tempfile.TemporaryDirectory() as directory, fake_browser(
                launches, closed, lambda *args, **kwargs: 'success'):
            bridge = ReadyBridge()
            escaped = None
            try:
                with patch('gui_backend.Store.close', failed_close):
                    execute(GuiRequest('run', 'chaoxing', 'course'), bridge, directory)
            except Exception as exc:
                escaped = exc
            self.assertIsNone(escaped, '清理异常也必须转换为 GUI done 事件')
            self.assertEqual(drain(bridge)[-1]['outcome'], 'error')
            retry = ReadyBridge()
            execute(GuiRequest('run', 'chaoxing', 'course'), retry, directory)
            self.assertEqual(drain(retry)[-1]['outcome'], 'success')


if __name__ == '__main__':
    unittest.main()
