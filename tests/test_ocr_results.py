"""Windows OCR 进程边界：空结果、重试和冲突不能混为一谈。"""
import base64
import json
import subprocess
import unittest
from unittest.mock import patch

from browser import ManualRequired
from quiz_ocr import _recognize


# A valid 1x1 PNG; OCR process responses are supplied at the subprocess boundary.
PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=')


def response(*texts):
    output = '\n'.join(json.dumps({'text': text}, ensure_ascii=False) for text in texts)
    return subprocess.CompletedProcess([], 0, output.encode('utf-8'), b'')


class OCRResultTests(unittest.TestCase):
    def setUp(self):
        _recognize.cache_clear()

    def tearDown(self):
        _recognize.cache_clear()

    def test_empty_readings_are_reported_as_unreadable_after_one_retry(self):
        with patch('quiz_ocr.subprocess.run', return_value=response('', '')) as run:
            with self.assertRaisesRegex(ManualRequired, '未识别出文字') as error:
                _recognize(PNG)
        self.assertNotIn('不一致', str(error.exception))
        self.assertEqual(run.call_count, 2)

    def test_empty_first_pass_is_recovered_by_larger_image(self):
        with patch('quiz_ocr.subprocess.run', side_effect=[response('', ''), response('错', '错')]) as run:
            self.assertEqual(_recognize(PNG), '错')
        self.assertEqual([c.kwargs['env']['COURSE_HELPER_OCR_SCALE'] for c in run.call_args_list], ['1', '2'])

    def test_partial_readings_are_not_combined_across_scales(self):
        with patch('quiz_ocr.subprocess.run', return_value=response('', '一')) as run:
            with self.assertRaisesRegex(ManualRequired, '有效文字识别结果不足'):
                _recognize(PNG)
        self.assertEqual(run.call_count, 1)

    def test_single_success_among_empty_results_is_not_confirmed(self):
        with patch('quiz_ocr.subprocess.run', side_effect=[response('错', ''), response('', '')]):
            with self.assertRaisesRegex(ManualRequired, '有效文字识别结果不足'):
                _recognize(PNG)

    def test_conflicting_readings_are_not_resolved_by_majority_vote(self):
        with patch('quiz_ocr.subprocess.run', return_value=response('对', '错')) as run:
            with self.assertRaisesRegex(ManualRequired, '不一致'):
                _recognize(PNG)
        self.assertEqual(run.call_count, 1)

    def test_conflicting_retry_results_are_not_accepted(self):
        with patch('quiz_ocr.subprocess.run', side_effect=[response('', ''), response('对', '错')]):
            with self.assertRaisesRegex(ManualRequired, '不一致'):
                _recognize(PNG)

    def test_one_reading_after_empty_retry_is_not_confirmed(self):
        with patch('quiz_ocr.subprocess.run', side_effect=[response('', ''), response('对', '')]):
            with self.assertRaisesRegex(ManualRequired, '有效文字识别结果不足'):
                _recognize(PNG)

    def test_consistent_readings_are_cached_without_another_process(self):
        with patch('quiz_ocr.subprocess.run', return_value=response('对', '对')) as run:
            self.assertEqual(_recognize(PNG), '对')
            self.assertEqual(_recognize(PNG), '对')
        self.assertEqual(run.call_count, 1)

    def test_malformed_process_output_is_reported_without_raw_exception(self):
        for payload in ({}, None, [], {'text': None}, {'text': 3}):
            with self.subTest(payload=payload):
                output = json.dumps({'text': '对'}) + '\n' + json.dumps(payload)
                result = subprocess.CompletedProcess([], 0, output.encode('utf-8'), b'')
                with patch('quiz_ocr.subprocess.run', return_value=result):
                    with self.assertRaisesRegex(ManualRequired, '文字识别不可用'):
                        _recognize(PNG)

    def test_retry_timeout_is_reported_without_unconfirmed_text(self):
        with patch('quiz_ocr.subprocess.run', side_effect=[response('', ''),
                   subprocess.TimeoutExpired('ocr', 20)]):
            with self.assertRaisesRegex(ManualRequired, '不可用或超时'):
                _recognize(PNG)

    def test_retry_uses_remaining_deadline_and_removes_temporary_image(self):
        with patch('quiz_ocr.time.monotonic', side_effect=[100, 101, 116]), \
             patch('quiz_ocr.subprocess.run', side_effect=[response('', ''), response('错', '错')]) as run:
            self.assertEqual(_recognize(PNG), '错')
        self.assertEqual([c.kwargs['timeout'] for c in run.call_args_list], [19, 4])
        from pathlib import Path
        self.assertFalse(Path(run.call_args.kwargs['env']['COURSE_HELPER_OCR_IMAGE']).exists())

    def test_missing_second_short_reading_is_invalid_process_output(self):
        with patch('quiz_ocr.subprocess.run', return_value=response('对')):
            with self.assertRaisesRegex(ManualRequired, '文字识别不可用'):
                _recognize(PNG)


if __name__ == '__main__':
    unittest.main()
