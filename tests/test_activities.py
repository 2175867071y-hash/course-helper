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
        item = Activity('quiz', 'quiz1', Mock(), Mock())
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
