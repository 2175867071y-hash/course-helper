import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from browser import ManualRequired


class ActivityTests(unittest.TestCase):
    def make(self, **kwargs):
        from activities import CourseActivities
        adapter = Mock()
        adapter.page.url = 'https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId=1'
        adapter.guard.return_value = None
        adapter.task_pending.return_value = False
        adapter.videos.return_value = []
        adapter.start_attempts = set()
        from browser import page_identity
        adapter.content_identity = lambda: page_identity(adapter.page.url)
        adapter.navigator = None
        course = CourseActivities(adapter, **kwargs)
        return course, adapter

    def test_no_activity_leaves_video_runner_in_charge(self):
        course, _ = self.make()
        with patch.object(course, '_detect', return_value=None):
            self.assertIsNone(course.process())
            self.assertEqual(course.identity(), '')

    def test_document_waits_then_scrolls_then_waits_at_last_page(self):
        from activities import Activity
        course, _ = self.make(document_seconds=10)
        item = Activity('document', 'doc1', Mock(), Mock())
        with patch.object(course, '_detect', return_value=item), \
             patch.object(course, '_document_step', side_effect=[False, True]) as step, \
             patch('activities.time.monotonic', side_effect=[0, 5, 10, 15, 20]):
            self.assertEqual(course.process(), 'working')
            self.assertEqual(course.process(), 'working')
            step.assert_not_called()
            self.assertEqual(course.process(), 'working')
            self.assertEqual(course.process(), 'working')
            self.assertEqual(course.process(), 'done')
            self.assertEqual(step.call_count, 2)

    def test_document_does_not_claim_done_when_platform_still_pending(self):
        from activities import Activity
        course, adapter = self.make(document_seconds=3)
        adapter.task_pending.return_value = True
        item = Activity('document', 'doc1', Mock(), Mock())
        with patch.object(course, '_detect', return_value=item), \
             patch.object(course, '_document_step', return_value=True), \
             patch('activities.time.monotonic', side_effect=[0, 3, 35]):
            self.assertEqual(course.process(), 'working')
            self.assertEqual(course.process(), 'working')
            with self.assertRaises(ManualRequired):
                course.process()

    def test_quiz_without_configuration_stops_before_editing(self):
        from activities import Activity
        from quiz_ai import QuizQuestion
        course, _ = self.make()
        with patch.object(course, '_detect', return_value=Activity('quiz', 'q1', Mock(), Mock())), \
             patch.object(course, '_read_quiz', return_value=(
                 [QuizQuestion('0', 'single', '1+1?', ['2', '3'])], ['binding'])), \
             patch('activities.solve_questions') as solve, \
             patch.object(course, '_fill_answers') as fill:
            with self.assertRaisesRegex(ManualRequired, 'AI'):
                course.process()
            solve.assert_not_called()
            fill.assert_not_called()

    def test_uai_quiz_uses_the_same_validated_ai_flow(self):
        from activities import Activity
        from quiz_ai import QuizQuestion, QuizAnswer
        course, adapter = self.make(ai_base_url='https://ai.example/v1',
                                    ai_model='example', ai_api_key='secret')
        adapter.platform = 'uai'
        question = QuizQuestion('0', 'single', '1+1?', ['2', '3'])
        with patch.object(course, '_detect', return_value=Activity('quiz', 'q1', Mock(), Mock())), \
             patch.object(course, '_read_quiz', return_value=([question], ['binding'])), \
             patch('activities.solve_questions', return_value=[QuizAnswer('0', [0], .95)]), \
             patch.object(course, '_fill_answers') as fill, \
             patch.object(course, '_verify_selection'), \
             patch.object(course, '_submit_quiz') as submit, \
             patch.object(course, '_success_signals', return_value=set()):
            self.assertEqual(course.process(), 'working')
            self.assertIn('q1', course.submitted)
            self.assertEqual(course.pending_quiz.key, 'q1')
            fill.assert_called_once()
            submit.assert_called_once()

    def test_quiz_request_status_counts_shared_material_once_and_stops_on_failure(self):
        from activities import Activity
        from quiz_ai import QuizAIError, QuizImage, QuizQuestion
        course, _ = self.make(ai_base_url='https://ai.example', ai_model='text',
                              vision_model='vision', ai_api_key='secret')
        shared = [QuizImage('material1', b'x'), QuizImage('material2', b'y')]
        questions = [QuizQuestion(str(i), 'single', 'See image', ['A', 'B'],
                                  images=[QuizImage(f'question{i}', b'z'), *shared])
                     for i in range(4)]
        with patch.object(course, '_detect', return_value=Activity('quiz', 'q', Mock(), Mock())), \
             patch.object(course, '_read_quiz', return_value=(questions, [])), \
             patch.object(course, '_emit') as emit, \
             patch('activities.solve_questions', side_effect=QuizAIError('测试超时')) as solve, \
             patch.object(course, '_fill_answers') as fill, patch.object(course, '_submit_quiz') as submit:
            with self.assertRaisesRegex(ManualRequired, '测试超时'):
                course.process()
            self.assertIn('图片 6', emit.call_args.args[0])
            solve.assert_called_once()
            fill.assert_not_called()
            submit.assert_not_called()

    def test_primary_failure_collects_web_answer_without_filling_or_submitting(self):
        from activities import Activity
        from quiz_ai import QuizImage, QuizQuestion
        from tests.test_quiz_crosscheck import RoundAI, choice
        web_result = {'answers': [choice('0', 'B')]}
        reviewer = Mock(spec=['with_updates'])
        def review_with_updates(data, images, update):
            update({'status': '正在接收豆包回答'})
            update({'status': '已收到回答', 'result': web_result})
            return web_result
        reviewer.with_updates.side_effect = review_with_updates
        course, _ = self.make(ai_base_url='https://ai.example/v1', ai_model='text',
            vision_model='vision', ai_api_key='TEST-KEY', quiz_cross_check=True, web_review=reviewer)
        question = QuizQuestion('0', 'single', '按截图作答', ['A', 'B'], page_letters=True,
            images=[QuizImage('question0', b'\x89PNG\r\n\x1a\nTEST')])
        reports = []
        with patch.object(course, '_detect', return_value=Activity('quiz', 'q1', Mock(), Mock())), \
             patch.object(course, '_read_quiz', return_value=([question], ['binding'])), \
             patch('urllib.request.build_opener', return_value=RoundAI([[choice('0', 'AB')]])), \
             patch.object(course, '_report', side_effect=reports.append), \
             patch.object(course, '_fill_answers') as fill, patch.object(course, '_submit_quiz') as submit:
            with self.assertRaisesRegex(ManualRequired, '未填写或提交'):
                course.process()
        reviewer.with_updates.assert_called_once()
        self.assertTrue(any('正在接收豆包回答' in report for report in reports))
        self.assertIn('豆包网页：B', reports[-1])
        fill.assert_not_called()
        submit.assert_not_called()
        self.assertEqual(course.submitted, {})

    def test_quiz_submit_timeout_is_not_retried(self):
        from activities import Activity
        from quiz_ai import QuizQuestion, QuizAnswer
        course, _ = self.make(ai_base_url='https://ai.example/v1', ai_model='example', ai_api_key='secret')
        item = Activity('quiz', 'q1', Mock(), Mock())
        question = QuizQuestion('0', 'single', '1+1?', ['2', '3'])
        with patch.object(course, '_detect', return_value=item), \
             patch.object(course, '_read_quiz', return_value=([question], ['binding'])), \
             patch('activities.solve_questions', return_value=[QuizAnswer('0', [0], .95)]), \
             patch.object(course, '_fill_answers'), \
             patch.object(course, '_verify_selection'), \
             patch.object(course, '_submit_quiz', side_effect=TimeoutError) as submit, \
             patch.object(course, '_quiz_success', return_value=False), \
             patch.object(course, '_success_signals', return_value=set()), \
             patch('activities.time.monotonic', side_effect=[0, 0, 1]):
            with self.assertRaises(ManualRequired):
                course.process()
            self.assertEqual(course.process(), 'working')
            submit.assert_called_once()

    def test_submission_needs_visible_success_before_advancing(self):
        from activities import Activity
        course, _ = self.make()
        course.submitted['q1'] = 0
        with patch.object(course, '_detect', return_value=Activity('quiz', 'q1', Mock(), Mock())), \
             patch.object(course, '_quiz_success', return_value=False), \
             patch('activities.time.monotonic', return_value=35):
            with self.assertRaisesRegex(ManualRequired, '提交'):
                course.process()

    def test_quiz_completion_inside_video_resumes_without_advancing_chapter(self):
        from activities import Activity
        course, adapter = self.make()
        adapter.videos.return_value = [SimpleNamespace(state=SimpleNamespace(key='v1', ended=False))]
        adapter.start_attempts.add('v1')
        course.submitted['q1'] = 0
        with patch.object(course, '_detect', return_value=Activity('quiz', 'q1', Mock(), Mock())), \
             patch.object(course, '_quiz_success', return_value=True):
            self.assertEqual(course.process(), 'working')
            self.assertNotIn('v1', adapter.start_attempts)
            self.assertIsNone(course.process())

    def test_verification_blocks_even_automatic_quiz(self):
        course, adapter = self.make()
        adapter.guard.return_value = '检测到验证码'
        with self.assertRaisesRegex(ManualRequired, '验证码'):
            course.process()

    def test_pending_submission_survives_question_disappearing(self):
        from activities import Activity
        course, _ = self.make()
        # 真实 Frame 的 parent_frame 链最终为 None；替身必须显式终止，否则向上遍历不会结束。
        frame = Mock()
        frame.parent_frame = None
        item = Activity('quiz', 'quiz1', frame, Mock())
        course.pending_quiz = item
        course.submitted[item.key] = 0
        with patch.object(course, '_detect', return_value=None), \
             patch.object(course, '_quiz_success', return_value=True):
            self.assertEqual(course.process(), 'done')
            self.assertIsNone(course.pending_quiz)

    def test_old_success_message_is_not_confirmation(self):
        from activities import Activity
        course, _ = self.make()
        item = Activity('quiz', 'quiz1', Mock(), Mock())
        course.success_before[item.key] = {'上一测验已提交'}
        with patch.object(course, '_success_signals', return_value={'上一测验已提交'}):
            self.assertFalse(course._quiz_success(item))
        with patch.object(course, '_success_signals', return_value={'上一测验已提交', '本次提交成功'}):
            self.assertTrue(course._quiz_success(item))

    def test_changed_question_after_submit_never_triggers_new_ai_call(self):
        from activities import Activity
        course, _ = self.make()
        old = Activity('quiz', 'old', Mock(), Mock())
        course.pending_quiz = old
        course.submitted[old.key] = 0
        with patch.object(course, '_detect', return_value=Activity('quiz', 'new', Mock(), Mock())), \
             patch.object(course, '_quiz_success', return_value=False), \
             patch('activities.time.monotonic', return_value=10), \
             patch('activities.solve_questions') as solve:
            self.assertEqual(course.process(), 'working')
            solve.assert_not_called()

    def test_completed_quiz_identity_does_not_hide_a_new_video(self):
        from activities import Activity
        course, adapter = self.make()
        item = Activity('quiz', 'old', Mock(), Mock(), adapter.page.url + '|frame')
        course.last_activity = item
        course.completed.add(item.key)
        adapter.videos.return_value = [SimpleNamespace(state=SimpleNamespace(ended=False))]
        with patch.object(course, '_detect', return_value=None):
            self.assertEqual(course.identity(), '')


if __name__ == '__main__':
    unittest.main()
