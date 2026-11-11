"""保留失败时真正送入 OCR 的图片与结果，成功识别不落盘。"""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from browser import ManualRequired
from ocr_diagnostics import record_failures
from quiz_ocr import _recognize
from tests.test_ocr_results import PNG, response


class OCRDiagnosticTests(unittest.TestCase):
    def setUp(self):
        _recognize.cache_clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        _recognize.cache_clear()
        self.temporary.cleanup()

    def report(self, directory=None):
        with ZipFile((directory or self.directory) / 'ocr-failure.zip') as archive:
            self.assertEqual(set(archive.namelist()), {'region.png', 'report.json'})
            self.assertEqual(archive.read('region.png'), PNG)
            return json.loads(archive.read('report.json'))

    def test_conflict_preserves_actual_input_and_both_readings_without_accepting_them(self):
        with record_failures(self.directory), \
             self.assertLogs(level='WARNING') as logs, \
             patch('quiz_ocr.subprocess.run', return_value=response('物体识别', '物体识另刂')):
            with self.assertRaisesRegex(ManualRequired, '不一致'):
                _recognize(PNG)
        report = self.report()
        self.assertEqual(report['readings'], ['物体识别', '物体识另刂'])
        self.assertEqual(report['image_size'], [1, 1])
        self.assertEqual(report['readings_truncated'], False)
        self.assertIn('不一致', report['error'])
        self.assertNotIn('物体识别', '\n'.join(logs.output))
        self.assertNotIn('物体识另刂', '\n'.join(logs.output))

    def test_success_does_not_store_question_image(self):
        with record_failures(self.directory), \
             patch('quiz_ocr.subprocess.run', return_value=response('物体识别', '物体识别')):
            self.assertEqual(_recognize(PNG), '物体识别')
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_new_failure_replaces_previous_bundle_and_bounds_reading_length(self):
        for readings in [('对', '错'), ('甲' * 21000, '')]:
            with record_failures(self.directory), \
                 patch('quiz_ocr.subprocess.run', return_value=response(*readings)):
                with self.assertRaises(ManualRequired):
                    _recognize(PNG)
        report = self.report()
        self.assertEqual(list(self.directory.iterdir()), [self.directory / 'ocr-failure.zip'])
        self.assertEqual(report['readings'], ['甲' * 256, ''])
        self.assertTrue(report['readings_truncated'])

    def test_disk_error_does_not_mask_recognition_error(self):
        with record_failures(self.directory), \
             patch('quiz_ocr.subprocess.run', return_value=response('对', '错')), \
             patch('ocr_diagnostics.os.replace', side_effect=PermissionError('locked')):
            with self.assertRaisesRegex(ManualRequired, '不一致'):
                _recognize(PNG)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_parallel_task_diagnostics_are_isolated_and_context_is_reset(self):
        barrier = threading.Barrier(2)
        errors = []
        def run_task(name):
            try:
                with record_failures(self.directory / name):
                    barrier.wait(timeout=3)
                    with self.assertRaises(ManualRequired):
                        _recognize(PNG)
                # A second failure outside the task must not use its old folder.
                threading.current_thread().name = name + '-outside'
                with self.assertRaises(ManualRequired):
                    _recognize(PNG)
            except Exception as exc:
                errors.append(exc)
        def output(*args, **kwargs):
            name = threading.current_thread().name
            return response(name, '不同文字')
        with patch('quiz_ocr.subprocess.run', side_effect=output):
            threads = [threading.Thread(target=run_task, args=(name,), name=name)
                       for name in ('course-a', 'course-b')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        for name in ('course-a', 'course-b'):
            self.assertEqual(self.report(self.directory / name)['readings'], [name, '不同文字'])


if __name__ == '__main__':
    unittest.main()
