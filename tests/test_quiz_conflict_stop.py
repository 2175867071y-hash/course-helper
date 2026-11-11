"""真实调度和双模型比对，仅替换浏览器 I/O 和外部回答。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from activities import Activity, CourseActivities
from main import Task, run_task
from quiz_ai import QuizImage, QuizQuestion
from tests.test_quiz_crosscheck import RoundAI, choice
from tests.test_runner import MixedCourse, StoreFixture, URL1


class QuizConflictStopTests(unittest.TestCase):
    def test_first_disagreement_terminates_course_before_fill_or_advance(self):
        for count, conflict_number in ((1, 1), (11, 7)):
            with self.subTest(count=count, conflict=conflict_number):
                self.check_disagreement(count, conflict_number)

    def test_missing_answer_does_not_hide_another_questions_confirmed_disagreement(self):
        self.check_disagreement(3, 2, missing_number=3)

    def check_disagreement(self, count, conflict_number, missing_number=None):
        adapter = MixedCourse(['quiz', 'video2'])
        reports = []
        control = SimpleNamespace(
            ask=Mock(return_value='q'), sleep=Mock(), check=lambda: None,
            emit=lambda kind, **data: reports.append(data['message']) if kind == 'quiz_report' else None)
        questions = [QuizQuestion(str(i), 'single', '截图', ['A', 'B'], page_letters=True,
                                  images=[QuizImage(f'question_{i}', b'\x89PNG\r\n\x1a\nTEST')])
                     for i in range(count)]
        primary = RoundAI([[choice(q.id, 'A') for q in questions[start:start+5]
                            if int(q.id)+1 != missing_number]
                           for start in range(0, count, 5)])
        reviewer = Mock(spec=['__call__'], side_effect=lambda data, images: {
            'answers': [choice(row['id'], 'B' if int(row['id'])+1 == conflict_number else 'A')
                        for row in data]})
        course = CourseActivities(adapter, control=control, ai_base_url='https://offline.invalid/v1',
                                  ai_api_key='test', vision_model='primary', screenshot_quiz=True,
                                  quiz_cross_check=True, web_review=reviewer)
        item = Activity('quiz', 'q', None, None)
        with StoreFixture() as store, patch('urllib.request.build_opener', return_value=primary), \
             patch.object(course, '_detect', return_value=item), patch.object(course, '_attempt', return_value='q'), \
             patch.object(course, '_read_quiz', return_value=(questions, {})), \
             patch.object(course, '_fill_answers') as fill, patch.object(course, '_submit_quiz') as submit:
            outcome = run_task(adapter, Task('course', 'chaoxing', URL1), store, 0, 90, control, activities=course)
        self.assertEqual(outcome, 'ai_disagreement')
        control.ask.assert_not_called()
        self.assertEqual(len(primary.requests), (conflict_number-1)//5 + 1)
        self.assertEqual(reviewer.call_count, len(primary.requests))
        fill.assert_not_called()
        submit.assert_not_called()
        self.assertEqual(adapter.position, 0)
        self.assertTrue(adapter.paused)
        question_report = reports[-1].split(f'第 {conflict_number} 题\n', 1)[1].split('\n\n', 1)[0]
        self.assertIn('原 AI（primary）：A', question_report)
        self.assertIn('豆包网页：B', question_report)
        self.assertIn('存在分歧', question_report)
