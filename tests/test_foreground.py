"""焦点暂停只等待用户返回，不激活其他任务的窗口或循环点击播放。"""
import itertools
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from browser import Adapter, ManualRequired
from core import Store, VideoState
from main import Task, run_task
from test_browser import DomPage


class ForegroundTests(unittest.TestCase):
    def setUp(self):
        self.page = DomPage()
        self.page.is_closed = lambda: False
        self.page.evaluate = Mock(return_value={'visible': True, 'focused': True})
        def probe(predicate, timeout):
            from playwright.sync_api import TimeoutError as BrowserTimeout
            self.assertEqual(timeout, 750)
            if '!==' not in predicate:
                self.focus_sample = self.page.evaluate()
            state = self.focus_sample
            if (not isinstance(state, dict) or type(state.get('visible')) is not bool
                    or type(state.get('focused')) is not bool):
                raise RuntimeError('无法读取焦点')
            focused = state['visible'] and state['focused']
            if focused != ('!==' not in predicate):
                raise BrowserTimeout('未符合谓词')
            return SimpleNamespace(dispose=lambda: None)
        self.page.wait_for_function = Mock(side_effect=probe)
        self.page.bring_to_front = Mock(side_effect=AssertionError('不得抢前台'))
        self.adapter = Adapter(self.page, 'chaoxing')
        self.adapter.navigator.identity = lambda: ''
        self.media = self.page.frames[0].video.data
        self.media.update(source='https://media.example/one.mp4', duration=30,
                          current=4, ready=4)
        self.key = self.adapter.videos()[0].state.key
        self.events = []
        self.control = SimpleNamespace(check=Mock(), sleep=Mock(),
            emit=lambda kind, **data: self.events.append((kind, data)))

    def test_runner_waits_for_focus_then_resumes_and_observes_natural_end(self):
        # 真实 runner/monitor/store，只有页面 I/O 用替身；暂停前已观察到进度。
        states = iter([VideoState('v', 0, 2, False, False),
                       VideoState('v', 1, 2, False, False),
                       VideoState('v', 1, 2, True, False),
                       VideoState('v', 1, 2, True, False),
                       VideoState('v', 2, 2, True, True)])
        self.adapter.videos = lambda: [SimpleNamespace(state=next(states))]
        self.adapter.play = Mock()
        self.adapter.pause = Mock()
        self.adapter.guard = lambda: None
        self.page.evaluate.side_effect = [
            {'visible': True, 'focused': False},
            {'visible': True, 'focused': False},
            {'visible': True, 'focused': True}]
        clock = itertools.count()
        store = Store(':memory:')
        self.addCleanup(store.close)
        with patch('main.time.monotonic', side_effect=lambda: next(clock)), \
             patch('main.ask_manual', return_value='q') as manual:
            outcome = run_task(self.adapter, Task('focus', 'chaoxing', self.page.url, 1),
                               store, 1, 90, self.control)
        self.assertEqual(outcome, 'limit', '焦点暂停不应转成普通人工阻断')
        manual.assert_not_called()
        self.adapter.play.assert_called_once()
        self.assertTrue(any('切回课程窗口' in event[1].get('message', '')
                            for event in self.events))
        self.page.bring_to_front.assert_not_called()

    def recover(self):
        return self.adapter.recover_foreground_pause(self.key, 1, self.control)

    def test_player_that_resumes_itself_keeps_consistent_progress_clock(self):
        states = iter([VideoState('v', 0, 7, False, False),
                       VideoState('v', 2, 7, False, False),
                       VideoState('v', 2, 7, True, False),
                       VideoState('v', 3, 7, False, False),
                       VideoState('v', 5, 7, False, False),
                       VideoState('v', 7, 7, True, True)])
        self.adapter.videos = lambda: [SimpleNamespace(state=next(states))]
        self.adapter.play = Mock()
        self.adapter.pause = Mock()
        self.adapter.guard = lambda: None
        self.page.evaluate.side_effect = [
            {'visible': True, 'focused': False},
            {'visible': True, 'focused': True}]
        clock = itertools.count(step=2)
        store = Store(':memory:')
        self.addCleanup(store.close)
        with patch('main.time.monotonic', side_effect=lambda: next(clock)), \
             patch('main.ask_manual', return_value='q') as manual:
            result = run_task(self.adapter, Task('focus', 'chaoxing', self.page.url, 1),
                              store, 2, 90, self.control)
        self.assertEqual(result, 'limit', '已自行恢复的正常进度不应误报跳变')
        manual.assert_not_called()
        self.adapter.play.assert_not_called()

    def test_foreground_pause_keeps_manual_flow(self):
        self.assertFalse(self.recover())
        self.assertEqual(self.page.frames[0].button.clicks, 0)

    def test_unknown_focus_does_not_infer_background(self):
        for value in ({}, {'visible': True}, None):
            self.page.evaluate.return_value = value
            self.assertFalse(self.recover())
        self.page.evaluate.side_effect = RuntimeError('页面正在切换')
        self.assertFalse(self.recover())

    def test_hidden_or_unfocused_waits_then_allows_exactly_one_resume(self):
        self.adapter.play(self.adapter.videos()[0])
        self.page.evaluate.side_effect = [
            {'visible': False, 'focused': False},
            {'visible': True, 'focused': False},
            {'visible': True, 'focused': True}]
        self.assertTrue(self.recover())
        self.assertEqual(self.page.frames[0].button.clicks, 2)
        with self.assertRaises(ManualRequired):
            self.adapter.play(self.adapter.videos()[0])
        self.assertEqual(self.page.frames[0].button.clicks, 2)
        self.page.bring_to_front.assert_not_called()

    def test_wait_can_be_stopped(self):
        self.page.evaluate.return_value = {'visible': False, 'focused': False}
        self.control.check.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.recover()
        self.assertEqual(self.page.frames[0].button.clicks, 0)

    def test_open_dialog_is_reported_before_focus_read(self):
        self.adapter.dialog_pending = True
        with self.assertRaisesRegex(ManualRequired, '弹窗'):
            self.recover()
        self.page.evaluate.assert_not_called()

    def test_dialog_opened_during_focus_read_is_reported(self):
        def pending_dialog():
            self.adapter.dialog_pending = True
            return {'visible': False, 'focused': False}
        self.page.evaluate.side_effect = lambda *args, **kwargs: pending_dialog()
        with self.assertRaisesRegex(ManualRequired, '弹窗'):
            self.adapter.foreground_state()
        self.assertEqual(self.page.frames[0].button.clicks, 0)

    def test_pause_does_not_read_dom_while_dialog_is_open(self):
        self.adapter.dialog_pending = True
        self.adapter.videos = Mock()
        self.adapter.pause()
        self.adapter.videos.assert_not_called()

    def test_both_focus_predicates_timing_out_means_unknown(self):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        self.page.wait_for_function.side_effect = BrowserTimeout('脚本未响应')
        self.assertIsNone(self.adapter.foreground_state())
        self.assertEqual(self.page.wait_for_function.call_count, 2)

    def test_already_playing_on_return_is_not_clicked_again(self):
        self.media['paused'] = False
        self.page.evaluate.side_effect = [
            {'visible': True, 'focused': False},
            {'visible': True, 'focused': True}]
        self.assertTrue(self.recover())
        self.assertEqual(self.page.frames[0].button.clicks, 0)

    def test_changed_video_or_ended_video_requires_confirmation(self):
        for mutation in ({'id': 'changed'}, {'ended': True}):
            with self.subTest(mutation=mutation):
                original = dict(self.media)
                self.media.update(mutation)
                self.page.evaluate.side_effect = [
                    {'visible': True, 'focused': False},
                    {'visible': True, 'focused': True}]
                with self.assertRaises(ManualRequired):
                    self.recover()
                self.media.clear()
                self.media.update(original)
        self.assertEqual(self.page.frames[0].button.clicks, 0)

    def test_login_on_return_is_not_bypassed(self):
        self.page.evaluate.side_effect = [
            {'visible': True, 'focused': False},
            {'visible': True, 'focused': True}]
        self.adapter.guard = lambda: '请先登录'
        with self.assertRaisesRegex(ManualRequired, '登录'):
            self.recover()
        self.assertEqual(self.page.frames[0].button.clicks, 0)


if __name__ == '__main__':
    unittest.main()
