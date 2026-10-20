"""多个真实工作线程配合 Tk 的隔离与生命周期验证，不打开课程。"""
import gc
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from gui import App
from gui_backend import Cancelled


class ParallelGuiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.started, self.answers = {}, []

        def runner(request, bridge, directory):
            self.started[request.task_id] = (request, bridge)
            try:
                bridge.emit('log', message=request.task_id + ' 日志')
                self.answers.append((request.task_id, bridge.ask('登录', request.task_id,
                    [('ready', '继续')])))
                bridge.emit('progress', current=25, duration=100, completed=2)
                bridge.emit('status', message='继续学习 ' + request.task_id)
                while True:
                    bridge.sleep(.01)
            except Cancelled:
                bridge.emit('done', outcome='cancelled')

        self.app = App(self.root, Path(self.directory.name), runner=runner)

    def tearDown(self):
        if hasattr(self.app, 'jobs'):
            for job in self.app.jobs.values():
                job.bridge.stop()
            for job in self.app.jobs.values():
                if job.worker:
                    job.worker.join(2)
        elif self.app.bridge:
            self.app.bridge.stop()
            self.app.worker.join(2)
        try:
            for callback in self.root.tk.splitlist(self.root.tk.call('after', 'info')):
                self.root.after_cancel(callback)
            self.root.destroy()
        except tk.TclError:
            pass
        # runner 闭包引用本测试对象；先断开 App 环，再在主线程回收 Tk 变量。
        self.app = None
        self.root = None
        gc.collect()
        self.directory.cleanup()

    def pump(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(.01)
        self.fail('界面未及时响应并发任务')

    def start_two(self):
        self.app.course_name.set('体育')
        self.app.start('run')
        self.app.platform.set('U校园 AI')
        self.app.course_name.set('英语')
        self.app.start('run')
        self.pump(lambda: len(self.started) == 2)

    def test_two_platforms_run_with_independent_prompts_and_logs(self):
        self.start_two()
        self.pump(lambda: all(job.prompt for job in self.app.jobs.values()))
        self.app.select_job('chaoxing:体育')
        self.assertEqual(self.app.prompt_message.cget('text'), 'chaoxing:体育')
        self.assertNotIn('uai:英语 日志', self.app.log.get('1.0', 'end'))
        self.app.answer('ready')
        self.pump(lambda: len(self.answers) == 1)
        self.assertEqual(self.answers, [('chaoxing:体育', 'ready')])
        self.assertIsNotNone(self.app.jobs['uai:英语'].prompt)
        self.app.select_job('uai:英语')
        self.assertEqual(self.app.prompt_message.cget('text'), 'uai:英语')
        self.assertNotIn('chaoxing:体育 日志', self.app.log.get('1.0', 'end'))

    def test_stop_selected_does_not_stop_other_task(self):
        self.start_two()
        self.app.select_job('chaoxing:体育')
        self.app.stop()
        self.pump(lambda: self.app.jobs['chaoxing:体育'].worker is None)
        self.assertFalse(self.started['uai:英语'][1].stopped)
        self.assertTrue(self.app.jobs['uai:英语'].worker.is_alive())
        self.assertEqual(str(self.app.start_button['state']), 'normal')

    def test_same_course_duplicate_is_rejected_even_inspect_or_other_browser(self):
        self.start_two()
        self.app.browser.set('Google Chrome')
        with patch('gui.messagebox.showerror') as error:
            self.app.start('inspect')
        self.assertTrue(error.called)
        self.assertEqual(len(self.app.jobs), 2)

    def test_background_progress_does_not_overwrite_selected_task(self):
        self.start_two()
        self.pump(lambda: all(job.prompt for job in self.app.jobs.values()))
        self.app.select_job('chaoxing:体育')
        bridge = self.started['uai:英语'][1]
        bridge.emit('progress', current=80, duration=100, completed=3)
        self.pump(lambda: self.app.jobs['uai:英语'].progress_value == 80)
        self.assertEqual(self.app.progress['value'], 0)
        self.app.select_job('uai:英语')
        self.assertEqual(self.app.progress['value'], 80)
        self.assertIn('3 个视频', self.app.progress_text.get())

    def test_settings_remain_available_and_new_jobs_get_new_ai_snapshot(self):
        self.app.ai_api_key.set('first-secret')
        self.app.course_name.set('体育')
        self.app.start('run')
        self.pump(lambda: 'chaoxing:体育' in self.started)
        self.assertEqual(str(self.app.settings_button['state']), 'normal')
        dialog = self.app.show_settings()
        self.assertIsNotNone(dialog)
        dialog.ai_api_key.set('second-secret')
        dialog.save()
        self.app.platform.set('U校园 AI')
        self.app.course_name.set('英语')
        self.app.start('run')
        self.pump(lambda: len(self.started) == 2)
        self.assertEqual(self.started['chaoxing:体育'][0].ai_api_key, 'first-secret')
        self.assertEqual(self.started['uai:英语'][0].ai_api_key, 'second-secret')
        saved = (Path(self.directory.name) / 'settings.json').read_text(encoding='utf-8')
        self.assertNotIn('secret', saved)

    def test_all_stop_releases_every_waiting_prompt(self):
        self.start_two()
        self.app.stop_all()
        self.pump(lambda: all(job.worker is None for job in self.app.jobs.values()))
        self.assertTrue(all(bridge.stopped for _, bridge in self.started.values()))
        self.assertIsNone(self.app.prompt_id)

    def test_close_waits_for_all_workers_and_destroys_root(self):
        self.start_two()
        with patch.object(self.root, 'destroy') as destroy:
            self.app.close()
            self.pump(lambda: destroy.called)
        self.assertTrue(all(job.worker is None for job in self.app.jobs.values()))

    def test_finished_task_can_restart_with_new_bridge(self):
        self.start_two()
        old_bridge = self.started['uai:英语'][1]
        self.app.stop()
        self.pump(lambda: self.app.jobs['uai:英语'].worker is None)
        self.app.start('run')
        self.pump(lambda: self.started['uai:英语'][1] is not old_bridge)
        self.assertEqual(len(self.app.jobs), 2)
        self.assertFalse(self.started['uai:英语'][1].stopped)

    def test_same_platform_different_courses_run_together(self):
        self.app.course_name.set('体育')
        self.app.start('run')
        self.app.course_name.set('历史')
        self.app.start('run')
        self.pump(lambda: len(self.started) == 2)
        self.assertTrue(all(job.worker.is_alive() for job in self.app.jobs.values()))

    def test_old_prompt_callback_cannot_answer_another_course(self):
        self.start_two()
        self.pump(lambda: all(job.prompt for job in self.app.jobs.values()))
        self.app.select_job('chaoxing:体育')
        old_id = self.app.prompt_id
        self.app.select_job('uai:英语')
        self.app.answer('ready', task_id='chaoxing:体育', request_id=old_id)
        self.assertEqual(self.answers, [])
        self.assertIsNotNone(self.app.jobs['uai:英语'].prompt)


if __name__ == '__main__':
    unittest.main()
