"""豆包复核离线测试，不访问实际账户或服务。"""
import json
import unittest
from unittest.mock import patch

from quiz_ai import QuizAIError, QuizQuestion, QuizImage, QuizBlank, QuizDragItem, solve_questions
from tests.test_quiz_crosscheck import RoundAI, choice


class WebConsensusTests(unittest.TestCase):
    def test_whole_question_batches_reach_both_reviewers_with_shared_material(self):
        from dataclasses import replace
        template = self.questions[0]
        shared = QuizImage('passage', template.images[0].data)
        self.questions = [replace(template, id=str(i), images=[
            QuizImage(f'question_{i}', shared.data), shared]) for i in range(12)]
        batches = [self.questions[:5], self.questions[5:10], self.questions[10:]]
        expected = [[choice(q.id, 'A') for q in batch] for batch in batches]
        ai = RoundAI(expected)
        observed, reports = [], []

        def review(data, images):
            part = len(observed)
            ids = [q['id'] for q in data]
            primary = ai.requests[part]['messages'][1]['content']
            self.assertEqual(json.loads(primary[0]['text'])['questions'], data)
            self.assertEqual([p['text'][5:] for p in primary[1:] if p['type']=='text'],
                             [p.id for p in images])
            self.assertEqual({p.id for p in images}, {'passage', *(f'question_{i}' for i in ids)})
            observed.append(ids)
            return {'answers': expected[part]}

        result = self.solve(review, ai=ai, report=reports.append)
        self.assertEqual(observed, [[q.id for q in batch] for batch in batches])
        self.assertEqual([a.id for a in result], [q.id for q in self.questions])
        for n in (1, 6, 12):
            self.assertIn(f'第 {n} 题', reports[-1])

    def test_live_failure_report_is_visible_before_review_returns(self):
        reports = []
        outer = self
        class LiveReview:
            def __call__(self, *args):
                raise AssertionError('应使用实时报告入口')
            def with_updates(self, data, images, update):
                update(dict(status='等待附件上传完成超时', result={'answers': []}, raw_reply='答案：B。'))
                outer.assertIn('等待附件上传完成超时', reports[-1])
                outer.assertIn('原 AI（primary）：B', reports[-1])
                outer.assertIn('答案：B。', reports[-1])
                outer.assertNotIn('豆包网页：正在请求', reports[-1])
                raise QuizAIError('已取消')
        with self.assertRaises(QuizAIError):
            self.solve(LiveReview(), report=reports.append)
        self.assertIn('答案：B。', reports[-1])

    def setUp(self):
        self.questions = [QuizQuestion('q', 'single', '按截图作答', ['C', 'A', 'B'],
            images=[QuizImage('image_q', b'\x89PNG\r\n\x1a\nTEST')], page_letters=True)]

    def solve(self, callback, ai=None, **options):
        self.ai = ai or RoundAI([[choice('q', 'B')]])
        with patch('urllib.request.build_opener', return_value=self.ai):
            return solve_questions(self.questions, 'https://ai.example/v1', 'text',
                'SECRET-KEY', vision_model='primary', cross_check=True,
                web_review=callback, **options)

    def test_blind_review_gets_same_data_and_bytes_without_key_or_first_answer(self):
        received = []
        def reviewer(data, images):
            received.append((data, images))
            return {'answers': [choice('q', 'B', .9)]}
        answers = self.solve(reviewer)
        self.assertEqual(answers[0].selected, [2])
        self.assertEqual(answers[0].confidence, .9)
        self.assertEqual(len(self.ai.requests), 1)
        self.assertNotIn('SECRET-KEY', repr(received))
        self.assertNotIn('confidence', repr(received))
        primary_data = json.loads(self.ai.requests[0]['messages'][1]['content'][0]['text'])
        self.assertEqual(primary_data['questions'], received[0][0])
        self.assertEqual(received[0][1], self.questions[0].images)

    def test_disagreement_does_not_request_tiebreak_or_return_partial_answers(self):
        with self.assertRaisesRegex(QuizAIError, '分歧.*1'):
            self.solve(lambda data, images: {'answers': [choice('q', 'A')]})
        self.assertEqual(len(self.ai.requests), 1)

    def test_matching_explicit_answers_allow_nonzero_confidence_from_either_provider(self):
        answers = self.solve(lambda *_: {'answers': [choice('q', 'B', .5)]},
                             ai=RoundAI([[choice('q', 'B', .4)]]))
        self.assertEqual(answers[0].selected, [2])
        self.assertEqual(answers[0].confidence, .4)

    def test_failed_or_abstaining_review_never_falls_back(self):
        for response in ({'answers': []}, {'answers': [choice('q', 'B', 0)]}):
            with self.subTest(response=response), self.assertRaises(QuizAIError):
                self.solve(lambda data, images: response)
        def fail(data, images):
            raise QuizAIError('豆包连接失败')
        with self.assertRaisesRegex(QuizAIError, '豆包连接失败'):
            self.solve(fail)

    def test_abstention_is_never_displayed_as_agreement(self):
        reports = []
        with self.assertRaises(QuizAIError):
            self.solve(lambda *_: {'answers': [choice('q', 'B', 0)]}, report=reports.append)
        self.assertIn('无法作答', reports[-1])
        self.assertNotIn('比对：一致', reports[-1])

    def test_stop_after_primary_prevents_web_request(self):
        stopped = False
        calls = []
        def stop():
            nonlocal stopped
            stopped = True
        def check():
            if stopped:
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.solve(lambda *args: calls.append(args),
                ai=RoundAI([[choice('q', 'B')]], after=stop), check=check)
        self.assertEqual(calls, [])

    def test_primary_failure_still_requests_blind_review_but_returns_no_answers(self):
        failures = (
            [choice('q', 'AB')],  # 单选题返回多个选项，与用户截图中的错误相同。
            [choice('q', 'Z')],
            [choice('q', 'B', 0)],
            [],
            TimeoutError('SECRET-KEY'),
        )
        for response in failures:
            with self.subTest(response=response):
                reports, calls = [], []
                def reviewer(data, images):
                    calls.append((data, images))
                    return {'answers': [choice('q', 'B')]}
                with self.assertRaises(QuizAIError) as caught:
                    self.solve(reviewer, ai=RoundAI([response]), report=reports.append)
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(self.ai.requests), 1)
                self.assertNotIn('confidence', repr(calls))
                self.assertEqual(calls[0][1], self.questions[0].images)
                self.assertIn('豆包网页：B', reports[-1])
                self.assertIn('未填写或提交', str(caught.exception))
                self.assertNotIn('尚未请求', reports[-1])
                self.assertNotIn('SECRET-KEY', '\n'.join(reports) + str(caught.exception))

    def test_both_provider_failures_remain_visible(self):
        reports, calls = [], []
        def reviewer(*args):
            calls.append(args)
            raise QuizAIError('豆包连接超时')
        with self.assertRaises(QuizAIError) as caught:
            self.solve(reviewer, ai=RoundAI([[choice('q', 'AB')]]), report=reports.append)
        self.assertEqual(len(calls), 1)
        for message in (reports[-1], str(caught.exception)):
            self.assertIn('AI 选项索引或选项数量无效', message)
            self.assertIn('豆包连接超时', message)

    def test_valid_primary_items_are_retained_when_another_item_is_invalid(self):
        self.questions.append(QuizQuestion('z', 'single', '第二题', ['A', 'B'], page_letters=True))
        reports = []
        primary = [choice('q', 'B'), choice('z', 'AB')]
        reviewed = [choice('q', 'B'), choice('z', 'A')]
        with self.assertRaises(QuizAIError):
            self.solve(lambda *args: {'answers': reviewed}, ai=RoundAI([primary]), report=reports.append)
        self.assertIn('原 AI（primary）：B', reports[-1])
        second = reports[-1].split('第 2 题')[1]
        self.assertIn('豆包网页：A', second)
        self.assertIn('AI 选项索引或选项数量无效', second)

    def test_stop_or_page_guard_after_invalid_primary_prevents_web_request(self):
        for failure in (KeyboardInterrupt(), QuizAIError('页面已切换')):
            with self.subTest(failure=type(failure).__name__):
                stopped, calls = False, []
                def stop():
                    nonlocal stopped
                    stopped = True
                def check():
                    if stopped:
                        raise failure
                with self.assertRaises(type(failure)) as caught:
                    self.solve(lambda *args: calls.append(args),
                        ai=RoundAI([[choice('q', 'AB')]], after=stop), check=check)
                self.assertIs(caught.exception, failure)
                self.assertEqual(calls, [])

    def test_pending_review_is_not_reported_as_failed(self):
        reports = []
        self.solve(lambda *args: {'answers': [choice('q', 'B')]}, report=reports.append)
        pending = next(text for text in reports if '豆包网页：尚未请求' in text)
        self.assertNotIn('豆包网页：未返回有效答案', pending)

    def test_stop_after_web_response_prevents_return(self):
        stopped = False
        def check():
            if stopped:
                raise KeyboardInterrupt()
        def reviewer(data, images):
            nonlocal stopped
            stopped = True
            return {'answers': [choice('q', 'B')]}
        with self.assertRaises(KeyboardInterrupt):
            self.solve(reviewer, check=check)

    def test_pause_report_shows_both_answers_and_confidence(self):
        reports = []
        with self.assertRaises(QuizAIError):
            self.solve(lambda *args: {'answers': [choice('q', 'A', .9)]}, report=reports.append)
        self.assertIn('原 AI（primary）：B', reports[-1])
        self.assertIn('豆包网页：A', reports[-1])
        self.assertIn('分歧', reports[-1])
        self.assertIn('自报置信度 0.9', reports[-1])
        self.assertNotIn('SECRET-KEY', '\n'.join(reports))

    def test_timeout_keeps_primary_answer_and_reports_missing_reviewer(self):
        reports = []
        def fail(*args):
            self.assertIn('原 AI（primary）：B', reports[-1])
            raise QuizAIError('豆包连接超时')
        with self.assertRaises(QuizAIError):
            self.solve(fail, report=reports.append)
        self.assertIn('原 AI（primary）：B', reports[-1])
        self.assertIn('豆包连接超时', reports[-1])
        self.assertIn('未返回有效答案', reports[-1])

    def test_disagreement_with_lower_confidence_is_still_terminal(self):
        from quiz_ai import QuizDisagreement
        reports = []
        with self.assertRaises(QuizDisagreement):
            self.solve(lambda *args: {'answers': [choice('q', 'A', .5)]}, report=reports.append)
        self.assertIn('豆包网页：A', reports[-1])
        self.assertIn('自报置信度 0.5', reports[-1])
        self.assertIn('存在分歧', reports[-1])

    def test_fill_and_drag_answers_are_shown_instead_of_only_match_status(self):
        self.questions = [QuizQuestion('q', 'fill', '[[blank]]', [], blanks=[QuizBlank('blank')]),
            QuizQuestion('d', 'drag', 'match', [], drag_sources=[QuizDragItem('s', 'S')],
                         drag_targets=[QuizDragItem('t', 'T')])]
        answer = [dict(id='q', filled={'blank': 'plunged into'}, confidence=.95),
                  dict(id='d', placements={'t': 's'}, confidence=.95)]
        reports = []
        self.solve(lambda *args: {'answers': answer}, ai=RoundAI([answer]), report=reports.append)
        self.assertEqual(reports[-1].count('blank=plunged into'), 2)
        self.assertEqual(reports[-1].count('t←s'), 2)

    def test_partial_web_batch_is_retained_on_failure(self):
        reports = []
        def fail(*args):
            error = QuizAIError('第二批上传失败')
            error.partial_result = {'answers': [choice('q', 'B')]}
            raise error
        with self.assertRaises(QuizAIError):
            self.solve(fail, report=reports.append)
        self.assertIn('豆包网页：B', reports[-1])
        self.assertIn('第二批上传失败', reports[-1])

    def test_api_rounds_report_each_return(self):
        reports = []
        ai = RoundAI([[choice('q', 'B')], [choice('q', 'A')], [choice('q', 'C')]])
        with patch('urllib.request.build_opener', return_value=ai), self.assertRaises(QuizAIError):
            solve_questions(self.questions, 'https://ai.example/v1', 'primary', 'TEST',
                vision_model='primary', cross_check=True, review_model='review', report=reports.append)
        for text in ('第 1 轮（primary）：B', '第 2 轮（review）：A', '第 3 轮（review）：C'):
            self.assertIn(text, reports[-1])


if __name__ == '__main__':
    unittest.main()
