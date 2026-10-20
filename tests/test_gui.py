"""自有界面的离线集成检查，不打开真实课程。"""
import gc
import json
import tempfile
import threading
import time
import tkinter as tk
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

from gui import App


class GuiTests(unittest.TestCase):
    def test_background_playback_is_saved_and_used_for_new_task(self):
        from gui_backend import GuiRequest
        self.app = app = App(self.root, Path(self.directory.name))
        self.assertTrue(hasattr(app, 'background_playback'), '缺少后台播放开关')
        self.assertTrue(app.background_playback.get())
        self.assertTrue(GuiRequest('run', 'uai', 'test').background_playback)
        app.background_playback.set(False)
        app._save_settings()
        app.background_playback.set(True)
        app._load_settings()
        self.assertFalse(app.background_playback.get())
        received = []
        app.runner = lambda request, bridge, directory: received.append(request)
        app.start('run')
        self.pump_until(lambda: bool(received))
        self.assertFalse(received[0].background_playback)

    def test_multimedia_settings_persist_both_keys_encrypted_and_reach_new_task(self):
        from gui import automation_values
        self.app = app = App(self.root, Path(self.directory.name))
        dialog = app.show_settings()
        self.assertTrue(hasattr(dialog, 'vision_model'), '图片和转写设置尚未加入界面')
        dialog.ai_base_url.set('https://api.deepseek.com')
        dialog.ai_model.set('existing-text-model')
        dialog.ai_api_key.set('TEXT-SYNTHETIC-KEY')
        dialog.vision_model.set('deepseek-flash')
        dialog.speech_base_url.set('https://speech.example/v1')
        dialog.speech_model.set('transcribe-example')
        dialog.speech_api_key.set('AUDIO-SYNTHETIC-KEY')
        self.assertEqual(dialog.speech_api_key_entry.cget('show'), '*')
        dialog.save()
        path = Path(self.directory.name) / 'settings.json'
        contents = path.read_text(encoding='utf-8')
        self.assertNotIn('TEXT-SYNTHETIC-KEY', contents)
        self.assertNotIn('AUDIO-SYNTHETIC-KEY', contents)
        self.assertNotIn('speech_api_key', json.loads(contents))
        app.ai_api_key.set('')
        app.speech_api_key.set('')
        app._load_settings()
        values = automation_values(app)
        self.assertEqual(values['ai_api_key'], 'TEXT-SYNTHETIC-KEY')
        self.assertEqual(values['speech_api_key'], 'AUDIO-SYNTHETIC-KEY')
        self.assertEqual(values['speech_model'], 'transcribe-example')
        self.assertEqual(values['vision_model'], 'deepseek-flash')
        dialog = app.show_settings()
        dialog.remember_api_key.set(False)
        dialog.save()
        self.assertNotIn('speech_api_key_protected', json.loads(path.read_text(encoding='utf-8')))

    def test_deepseek_picture_model_default_does_not_change_text_model(self):
        from gui import automation_values
        self.app = app = App(self.root, Path(self.directory.name))
        self.assertTrue(hasattr(app, 'vision_model'), '图片设置尚未加入')
        app.ai_base_url.set('https://api.deepseek.com/v1')
        app.ai_model.set('my-existing-model')
        values = automation_values(app)
        self.assertEqual(values['vision_model'], 'deepseek-flash')
        self.assertEqual(values['ai_model'], 'my-existing-model')
        app.ai_base_url.set('https://other.example/v1')
        self.assertEqual(automation_values(app)['vision_model'], '')

    def test_speech_settings_reject_invalid_endpoint_without_changing_saved_key(self):
        self.app = app = App(self.root, Path(self.directory.name))
        dialog = app.show_settings()
        self.assertTrue(hasattr(dialog, 'speech_base_url'), '语音设置尚未加入')
        dialog.speech_base_url.set('https://user:password@example.com/')
        with patch('gui.messagebox.showerror') as error:
            dialog.save()
        self.assertTrue(error.called)
        self.assertEqual(app.speech_base_url.get(), '')

    def setUp(self):
        self.app = None
        self.directory = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        if self.app is not None:
            for job in self.app.jobs.values():
                job.bridge.stop()
            for job in self.app.jobs.values():
                if job.worker is not None:
                    job.worker.join(2)
        for callback in self.root.tk.splitlist(self.root.tk.call('after', 'info')):
            self.root.after_cancel(callback)
        self.root.destroy()
        # Tk 控件存在 Python 引用环，不能留到后续工作线程触发 GC。
        self.app = None
        self.root = None
        gc.collect()
        self.directory.cleanup()

    def pump_until(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(0.01)
        self.fail('界面未及时响应工作线程')

    def test_stop_during_prompt_finishes_without_hanging_ui(self):
        from gui_backend import Cancelled

        def worker(request, bridge, directory):
            try:
                bridge.ask('登录', '打开课程后继续', [('ready', '继续')])
            except Cancelled:
                pass
            finally:
                bridge.emit('done', outcome='cancelled')

        self.app = app = App(self.root, Path(self.directory.name), runner=worker)
        app.start('run')
        self.pump_until(lambda: app.prompt_id is not None)
        app.stop()
        self.pump_until(lambda: app.worker is None)
        self.assertEqual(str(app.start_button['state']), 'normal')
        self.assertEqual(str(app.stop_button['state']), 'disabled')
        self.assertIsNone(app.prompt_id)

    def test_current_prompt_reply_reaches_worker(self):
        answers = []

        def worker(request, bridge, directory):
            answers.append(bridge.ask('登录', '打开课程', [('ready', '继续')]))
            bridge.emit('done', outcome='inspected')

        self.app = app = App(self.root, Path(self.directory.name), runner=worker)
        app.start('inspect')
        self.pump_until(lambda: app.prompt_id is not None)
        app.answer('ready')
        self.pump_until(lambda: app.worker is None)
        self.assertEqual(answers, ['ready'])

    def test_inspection_displays_question_counts_and_reason(self):
        def worker(request, bridge, directory):
            bridge.emit('inspection', report={'frames': [{'videos': [], 'quiz': {
                'questions': 10, 'blanks': 10, 'status': 'manual',
                'message': '这是个人自评题（1—7 分），请按实际情况填写。'}}]})
            bridge.emit('done', outcome='inspected')
        self.app = app = App(self.root, Path(self.directory.name), runner=worker)
        app.start('inspect')
        self.pump_until(lambda: app.worker is None)
        text = app.log.get('1.0', 'end')
        self.assertIn('10 道题', text)
        self.assertIn('10 个填空', text)
        self.assertIn('个人自评', text)

    def test_queued_prompt_cannot_reappear_after_stop(self):
        from gui import CourseJob
        from gui_backend import GuiRequest
        self.app = app = App(self.root, Path(self.directory.name))
        job = CourseJob(GuiRequest('run', 'chaoxing', 'chaoxing:test'))
        app.jobs[job.request.task_id] = job
        app.task_table.insert('', 'end', iid=job.request.task_id)
        app.select_job(job.request.task_id)
        app.bridge.stop()
        app.handle_event({'kind': 'prompt', 'request_id': 1, 'title': '登录',
                          'message': '已取消的提示', 'choices': [('ready', '继续')]})
        self.assertIsNone(app.prompt_id)

    def request_runner(self, requests):
        def worker(request, bridge, directory):
            requests.append(request)
            bridge.emit('done', outcome='limit')
        return worker

    def test_old_one_video_setting_upgrades_to_continuous_request(self):
        settings = Path(self.directory.name) / 'settings.json'
        settings.write_text(json.dumps({'maximum': 1}), encoding='utf-8')
        requests = []
        self.app = app = App(self.root, Path(self.directory.name), runner=self.request_runner(requests))
        app.start('run')
        self.pump_until(lambda: app.worker is None)
        self.assertEqual(requests[0].max_chapters, 0)
        self.assertTrue(app.continuous.get())
        self.assertEqual(str(app.maximum_input['state']), 'disabled')
        self.assertTrue(json.loads(settings.read_text(encoding='utf-8'))['continuous'])

    def test_continuous_ignores_invalid_unused_video_limit(self):
        requests = []
        self.app = app = App(self.root, Path(self.directory.name), runner=self.request_runner(requests))
        app.maximum.set('not a number')
        with patch('gui.messagebox.showerror') as error:
            app.start('run')
            self.pump_until(lambda: app.worker is None)
        self.assertFalse(error.called)
        self.assertEqual(requests[0].max_chapters, 0)

    def test_generic_course_link_is_accepted_and_selection_persists(self):
        requests = []
        self.app = app = App(self.root, Path(self.directory.name), runner=self.request_runner(requests))
        app.platform.set('通用课程（自动识别链接）')
        app.url.set('https://courses.example.test/lesson/one')
        with patch('gui.messagebox.showerror') as error:
            app.start('run')
            self.pump_until(lambda: app.worker is None)
        self.assertFalse(error.called)
        self.assertEqual(requests[0].platform, 'generic')
        self.assertEqual(requests[0].url, 'https://courses.example.test/lesson/one')
        saved = json.loads((Path(self.directory.name) / 'settings.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['platform'], '通用课程（自动识别链接）')

    def test_limited_mode_validates_bounds_and_passes_selected_limit(self):
        requests = []
        self.app = app = App(self.root, Path(self.directory.name), runner=self.request_runner(requests))
        self.assertTrue(hasattr(app, 'continuous_toggle'), '需要可操作的整课学习开关')
        app.continuous_toggle.invoke()
        self.assertEqual(str(app.maximum_input['state']), 'normal')
        for invalid in ('0', '-1', '1001', '1.5', 'abc'):
            with self.subTest(invalid=invalid), patch('gui.messagebox.showerror') as error:
                app.maximum.set(invalid)
                app.start('run')
                self.assertIsNone(app.worker)
                self.assertTrue(error.called)
        self.assertEqual(requests, [])
        for valid in ('1', '1000'):
            app.maximum.set(valid)
            app.start('run')
            self.pump_until(lambda: app.worker is None)
            self.assertEqual(requests[-1].max_chapters, int(valid))

    def test_settings_save_without_remembering_persists_preferences_only(self):
        self.app = app = App(self.root, Path(self.directory.name))
        self.assertTrue(hasattr(app, 'show_settings'), '需要自动学习设置窗口')
        dialog = app.show_settings()
        self.assertEqual(dialog.ai_api_key_entry.cget('show'), '*')
        dialog.auto_documents.set(False)
        dialog.auto_quiz.set(False)
        dialog.quiz_submit.set(False)
        dialog.document_seconds.set('23')
        dialog.ai_base_url.set('https://ai.example.test/v1')
        dialog.ai_model.set('example-model')
        dialog.ai_api_key.set('sk-local-only-test-secret')
        dialog.remember_api_key.set(False)
        dialog.save()
        settings_path = Path(self.directory.name) / 'settings.json'
        contents = settings_path.read_text(encoding='utf-8')
        saved = json.loads(contents)
        self.assertFalse(saved['auto_documents'])
        self.assertFalse(saved['auto_quiz'])
        self.assertFalse(saved['quiz_submit'])
        self.assertEqual(saved['document_seconds'], 23)
        self.assertEqual(saved['ai_base_url'], 'https://ai.example.test/v1')
        self.assertEqual(saved['ai_model'], 'example-model')
        self.assertFalse(saved['remember_api_key'])
        self.assertNotIn('ai_api_key_protected', saved)
        self.assertNotIn('ai_api_key', saved)
        self.assertNotIn('sk-local-only-test-secret', contents)
        self.assertEqual(app.ai_api_key.get(), 'sk-local-only-test-secret')
        app.ai_api_key.set('')
        app.quiz_submit.set(True)
        app._load_settings()
        self.assertEqual(app.ai_api_key.get(), '')
        self.assertFalse(app.auto_documents.get())
        self.assertFalse(app.quiz_submit.get())
        self.assertEqual(app.document_seconds.get(), '23')

    def test_remembered_key_is_encrypted_and_restored_with_settings(self):
        self.app = app = App(self.root, Path(self.directory.name))
        dialog = app.show_settings()
        self.assertTrue(dialog.remember_api_key.get())
        dialog.ai_api_key.set('sk-remember-roundtrip-test')
        dialog.save()
        contents = (Path(self.directory.name) / 'settings.json').read_text(encoding='utf-8')
        saved = json.loads(contents)
        self.assertTrue(saved['remember_api_key'])
        self.assertTrue(saved['ai_api_key_protected'])
        self.assertNotIn('sk-remember-roundtrip-test', contents)
        self.assertNotIn('ai_api_key', saved)
        app.ai_api_key.set('')
        app._load_settings()
        self.assertEqual(app.ai_api_key.get(), 'sk-remember-roundtrip-test')
        self.assertNotIn('sk-remember-roundtrip-test', app.log.get('1.0', 'end'))

    def test_unchecking_remember_removes_saved_key_but_keeps_current_session(self):
        self.app = app = App(self.root, Path(self.directory.name))
        dialog = app.show_settings()
        dialog.ai_api_key.set('sk-to-forget-test')
        dialog.save()
        dialog = app.show_settings()
        dialog.remember_api_key.set(False)
        dialog.save()
        saved = json.loads((Path(self.directory.name) / 'settings.json').read_text(encoding='utf-8'))
        self.assertFalse(saved['remember_api_key'])
        self.assertNotIn('ai_api_key_protected', saved)
        self.assertEqual(app.ai_api_key.get(), 'sk-to-forget-test')
        app.ai_api_key.set('')
        app._load_settings()
        self.assertEqual(app.ai_api_key.get(), '')
        self.assertFalse(app.remember_api_key.get())

    def test_remembered_key_is_loaded_when_app_is_recreated(self):
        self.app = app = App(self.root, Path(self.directory.name))
        dialog = app.show_settings()
        dialog.ai_base_url.set('https://ai.example.test/v1')
        dialog.ai_model.set('restart-test-model')
        dialog.ai_api_key.set('sk-app-restart-test')
        dialog.save()
        del dialog
        for callback in self.root.tk.splitlist(self.root.tk.call('after', 'info')):
            self.root.after_cancel(callback)
        self.root.destroy()
        self.app = self.root = None
        del app
        gc.collect()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = app = App(self.root, Path(self.directory.name))
        self.assertTrue(app.remember_api_key.get())
        self.assertEqual(app.ai_base_url.get(), 'https://ai.example.test/v1')
        self.assertEqual(app.ai_model.get(), 'restart-test-model')
        self.assertEqual(app.ai_api_key.get(), 'sk-app-restart-test')
        dialog = app.show_settings()
        self.assertEqual(dialog.ai_api_key_entry.get(), 'sk-app-restart-test')
        self.assertEqual(dialog.ai_api_key_entry.cget('show'), '*')
        dialog.destroy()

    def test_replacing_and_clearing_remembered_key(self):
        self.app = app = App(self.root, Path(self.directory.name))
        for key in ('sk-first-test', 'sk-replacement-test', ''):
            with self.subTest(key=key):
                dialog = app.show_settings()
                dialog.ai_api_key.set(key)
                dialog.save()
                app.ai_api_key.set('')
                app._load_settings()
                self.assertEqual(app.ai_api_key.get(), key)
        saved = json.loads((Path(self.directory.name) / 'settings.json').read_text(encoding='utf-8'))
        self.assertNotIn('ai_api_key_protected', saved)

    def test_key_encryption_failure_keeps_saved_settings_and_dialog_draft(self):
        self.app = app = App(self.root, Path(self.directory.name))
        app._save_settings()
        path = Path(self.directory.name) / 'settings.json'
        before = path.read_bytes()
        dialog = app.show_settings()
        dialog.ai_api_key.set('sk-unsaved-test')
        dialog.quiz_submit.set(False)
        with patch('gui.protect_api_key', create=True, side_effect=OSError('加密不可用')), \
                patch('gui.messagebox.showerror') as error:
            dialog.save()
        error.assert_called_once()
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(dialog.ai_api_key.get(), 'sk-unsaved-test')
        self.assertEqual(app.ai_api_key.get(), '')
        self.assertTrue(app.quiz_submit.get())
        self.assertEqual(path.read_bytes(), before)

    def test_atomic_settings_write_failure_preserves_previous_key(self):
        self.app = app = App(self.root, Path(self.directory.name))
        dialog = app.show_settings()
        dialog.ai_api_key.set('sk-previous-test')
        dialog.save()
        path = Path(self.directory.name) / 'settings.json'
        before = path.read_bytes()
        dialog = app.show_settings()
        dialog.remember_api_key.set(False)
        with patch('gui.os.replace', side_effect=OSError('写入失败')), \
                patch('gui.messagebox.showerror') as error:
            dialog.save()
        error.assert_called_once()
        self.assertTrue(dialog.winfo_exists())
        self.assertTrue(app.remember_api_key.get())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(Path(self.directory.name).glob('.settings-*.tmp')), [])

    def test_unreadable_saved_key_does_not_break_startup_or_get_silently_erased(self):
        path = Path(self.directory.name) / 'settings.json'
        broken = 'dpapi:v1:broken-test-ciphertext'
        path.write_text(json.dumps({'remember_api_key': True, 'ai_api_key_protected': broken,
                                    'ai_model': 'test-model'}), encoding='utf-8')
        self.app = app = App(self.root, Path(self.directory.name))
        self.assertEqual(app.ai_api_key.get(), '')
        self.assertEqual(app.ai_model.get(), 'test-model')
        self.assertIn('无法读取', app.status.get())
        self.assertNotIn(broken, app.log.get('1.0', 'end'))
        app._save_settings()
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['ai_api_key_protected'], broken)
        dialog = app.show_settings()
        dialog.remember_api_key.set(False)
        dialog.save()
        self.assertNotIn('ai_api_key_protected', json.loads(path.read_text(encoding='utf-8')))
        self.assertNotIn('无法读取', app.status.get())

    def test_recovering_saved_key_does_not_overwrite_current_task_status(self):
        path = Path(self.directory.name) / 'settings.json'
        path.write_text(json.dumps({'remember_api_key': True,
                                    'ai_api_key_protected': 'dpapi:v1:broken'}), encoding='utf-8')
        self.app = app = App(self.root, Path(self.directory.name))
        app.status.set('正在观察正常播放进度')
        dialog = app.show_settings()
        dialog.ai_api_key.set('sk-recovered-test')
        dialog.save()
        self.assertEqual(app.status.get(), '正在观察正常播放进度')
        app.ai_api_key.set('')
        app._load_settings()
        self.assertEqual(app.ai_api_key.get(), 'sk-recovered-test')

    def test_settings_cancel_discards_edits(self):
        self.app = app = App(self.root, Path(self.directory.name))
        self.assertTrue(hasattr(app, 'show_settings'), '需要自动学习设置窗口')
        dialog = app.show_settings()
        dialog.auto_documents.set(False)
        dialog.quiz_submit.set(False)
        dialog.ai_api_key.set('discarded-secret')
        dialog.remember_api_key.set(False)
        dialog.destroy()
        self.assertTrue(app.auto_documents.get())
        self.assertTrue(app.quiz_submit.get())
        self.assertEqual(app.ai_api_key.get(), '')
        self.assertTrue(app.remember_api_key.get())
        self.assertFalse((Path(self.directory.name) / 'settings.json').exists())

    def test_closed_settings_release_tk_variables_before_background_gc(self):
        self.app = app = App(self.root, Path(self.directory.name))
        original_del = tk.Variable.__del__
        owner = threading.get_ident()
        collected_on = []
        tracked_ids = set()
        expected_count = 0

        def tracked_del(variable):
            if id(variable) in tracked_ids:
                collected_on.append(threading.get_ident())
            original_del(variable)

        was_enabled = gc.isenabled()
        gc.disable()
        try:
            with patch.object(tk.Variable, '__del__', tracked_del):
                for _ in range(3):
                    dialog = app.show_settings()
                    variables = [value for value in vars(dialog).values()
                                 if isinstance(value, tk.Variable)]
                    tracked_ids.update(id(value) for value in variables)
                    expected_count += len(variables)
                    references = [weakref.ref(value) for value in variables]
                    del variables
                    dialog.destroy()
                    del dialog
                    self.assertTrue(all(reference() is None for reference in references),
                                    '已关闭设置窗口不能把 Tk 变量留给后台 GC')
                worker = threading.Thread(target=gc.collect)
                worker.start()
                worker.join(2)
                self.assertFalse(worker.is_alive())
            self.assertEqual(len(collected_on), expected_count)
            self.assertEqual(set(collected_on), {owner})
        finally:
            gc.collect()
            if was_enabled:
                gc.enable()

    def test_settings_validate_document_seconds_without_losing_draft(self):
        self.app = app = App(self.root, Path(self.directory.name))
        self.assertTrue(hasattr(app, 'show_settings'), '需要自动学习设置窗口')
        dialog = app.show_settings()
        for invalid in ('2', '121', 'abc', 'nan', 'inf'):
            with self.subTest(invalid=invalid), patch('gui.messagebox.showerror') as error:
                dialog.document_seconds.set(invalid)
                dialog.save()
                self.assertTrue(error.called)
                self.assertTrue(dialog.winfo_exists())
                self.assertEqual(app.document_seconds.get(), '10')
        dialog.document_seconds.set('3')
        dialog.save()
        self.assertEqual(app.document_seconds.get(), '3')
        dialog = app.show_settings()
        dialog.document_seconds.set('120')
        dialog.save()
        self.assertEqual(app.document_seconds.get(), '120')

    def test_settings_reject_unsafe_ai_endpoints(self):
        self.app = app = App(self.root, Path(self.directory.name))
        self.assertTrue(hasattr(app, 'show_settings'), '需要自动学习设置窗口')
        dialog = app.show_settings()
        for invalid in ('https://name:secret@ai.example.test/v1', 'http://localhost/v1',
                        'https://ai.example.test/v1?key=secret', 'https://ai.example.test/#x',
                        'not a url', 'https://ai.example.test:invalid/v1'):
            with self.subTest(invalid=invalid), patch('gui.messagebox.showerror') as error:
                dialog.ai_base_url.set(invalid)
                dialog.save()
                self.assertTrue(error.called)
        self.assertEqual(app.ai_base_url.get(), '')
        self.assertFalse((Path(self.directory.name) / 'settings.json').exists())

    def test_requests_include_auto_settings_and_missing_ai_does_not_block_video(self):
        requests = []
        self.app = app = App(self.root, Path(self.directory.name), runner=self.request_runner(requests))
        self.assertTrue(hasattr(app, 'ai_api_key'), '需要可传递给当前任务的 API key')
        app.ai_base_url.set('https://ai.example.test/v1')
        app.ai_model.set('example-model')
        app.ai_api_key.set('memory-only-key')
        app.document_seconds.set('12')
        app.start('run')
        self.pump_until(lambda: app.worker is None)
        request = requests[-1]
        self.assertTrue(request.auto_documents)
        self.assertTrue(request.auto_quiz)
        self.assertTrue(request.quiz_submit)
        self.assertEqual(request.document_seconds, 12)
        self.assertEqual(request.ai_base_url, 'https://ai.example.test/v1')
        self.assertEqual(request.ai_model, 'example-model')
        self.assertEqual(request.ai_api_key, 'memory-only-key')
        app.ai_api_key.set('')
        app.start('run')
        self.pump_until(lambda: app.worker is None)
        self.assertEqual(requests[-1].ai_api_key, '')
        self.assertEqual(len(requests), 2)

    def test_running_keeps_new_task_settings_available(self):
        requests = []
        self.app = app = App(self.root, Path(self.directory.name), runner=self.request_runner(requests))
        self.assertTrue(hasattr(app, 'settings_button'), '需要自动学习设置按钮')
        app.start('run')
        self.assertEqual(str(app.settings_button['state']), 'normal')
        dialog = app.show_settings()
        self.assertIsNotNone(dialog)
        dialog.destroy()
        self.pump_until(lambda: app.worker is None)
        self.assertEqual(str(app.settings_button['state']), 'normal')
        self.assertEqual(str(app.maximum_input['state']), 'disabled')

    def test_bad_saved_values_fall_back_and_saved_key_is_not_loaded(self):
        settings = {'continuous': 'false', 'maximum': 1, 'auto_documents': 'false',
                    'auto_quiz': False, 'quiz_submit': 'false',
                    'document_seconds': 999, 'ai_api_key': 'do-not-load',
                    'ai_base_url': 'https://ai.example.test/v1', 'ai_model': 'example-model'}
        (Path(self.directory.name) / 'settings.json').write_text(
            json.dumps(settings), encoding='utf-8')
        self.app = app = App(self.root, Path(self.directory.name))
        self.assertTrue(hasattr(app, 'continuous'), '旧设置应迁移为整门课模式')
        self.assertTrue(app.continuous.get())
        self.assertTrue(app.auto_documents.get())
        self.assertFalse(app.auto_quiz.get())
        self.assertTrue(app.quiz_submit.get())
        self.assertEqual(app.document_seconds.get(), '10')
        self.assertEqual(app.ai_api_key.get(), '')

    def test_old_settings_keep_automatic_submission_enabled(self):
        settings = Path(self.directory.name) / 'settings.json'
        settings.write_text(json.dumps({'auto_quiz': True}), encoding='utf-8')
        requests = []
        self.app = app = App(self.root, Path(self.directory.name), runner=self.request_runner(requests))
        self.assertTrue(app.quiz_submit.get())
        app.start('run')
        self.pump_until(lambda: app.worker is None)
        self.assertTrue(requests[0].quiz_submit)
        self.assertTrue(json.loads(settings.read_text(encoding='utf-8'))['quiz_submit'])

    def test_submission_setting_is_snapshotted_for_each_running_task(self):
        from gui_backend import Cancelled
        requests = []

        def worker(request, bridge, directory):
            requests.append(request)
            try:
                bridge.ask('运行中', '保留当前任务设置', [('continue', '继续')])
            except Cancelled:
                pass
            finally:
                bridge.emit('done', outcome='cancelled')

        self.app = app = App(self.root, Path(self.directory.name), runner=worker)
        try:
            app.course_name.set('第一门课')
            app.start('run')
            self.pump_until(lambda: len(requests) == 1)
            self.assertTrue(requests[0].quiz_submit)
            dialog = app.show_settings()
            dialog.quiz_submit.set(False)
            dialog.save()
            self.assertTrue(requests[0].quiz_submit, '保存设置不能改变已运行任务')
            app.course_name.set('第二门课')
            app.start('run')
            self.pump_until(lambda: len(requests) == 2)
            self.assertFalse(requests[1].quiz_submit)
            self.assertTrue(requests[0].quiz_submit)
        finally:
            app.stop_all()
            self.pump_until(lambda: all(job.worker is None for job in app.jobs.values()))

    def test_settings_save_error_preserves_submission_preference(self):
        self.app = app = App(self.root, Path(self.directory.name))
        dialog = app.show_settings()
        dialog.quiz_submit.set(False)
        with patch.object(app, '_save_settings', side_effect=OSError('写入失败')), \
                patch('gui.messagebox.showerror') as error:
            dialog.save()
        self.assertTrue(error.called)
        self.assertTrue(app.quiz_submit.get())
        self.assertFalse(dialog.quiz_submit.get(), '保留未成功保存的界面草稿')
        self.assertTrue(dialog.winfo_exists())

    def test_backend_document_and_quiz_statuses_reach_ui(self):
        from gui import CourseJob
        from gui_backend import GuiRequest
        self.app = app = App(self.root, Path(self.directory.name))
        job = CourseJob(GuiRequest('run', 'chaoxing', 'chaoxing:test'))
        app.jobs[job.request.task_id] = job
        app.task_table.insert('', 'end', iid=job.request.task_id)
        app.select_job(job.request.task_id)
        for status in ('读取文档', 'AI处理小测'):
            app.handle_event({'kind': 'status', 'message': status})
            self.assertEqual(app.status.get(), status)

    def test_minimum_window_sizes_keep_all_action_buttons_visible(self):
        self.root.tk.call('tk', 'scaling', 2.0)
        self.app = app = App(self.root, Path(self.directory.name))
        self.root.deiconify()
        width, height = self.root.minsize()
        self.root.geometry(f'{width}x{height}')
        self.root.update()

        def check_buttons(container):
            for widget in container.winfo_children():
                if widget.winfo_class() == 'TButton':
                    self.assertTrue(widget.winfo_ismapped(), widget.cget('text'))
                    self.assertGreater(widget.winfo_width(), 1, widget.cget('text'))
                    self.assertGreater(widget.winfo_height(), 1, widget.cget('text'))
                    self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth(),
                                            widget.cget('text'))
                    left = widget.winfo_rootx()
                    top = widget.winfo_rooty()
                    self.assertLessEqual(left + widget.winfo_width(),
                                         window.winfo_rootx() + window.winfo_width(),
                                         widget.cget('text'))
                    self.assertLessEqual(top + widget.winfo_height(),
                                         window.winfo_rooty() + window.winfo_height(),
                                         widget.cget('text'))
                check_buttons(widget)

        window = self.root
        check_buttons(window)
        dialog = app.show_settings()
        width, height = dialog.minsize()
        dialog.geometry(f'{width}x{height}')
        self.root.update()
        window = dialog
        check_buttons(window)


if __name__ == '__main__':
    unittest.main()
