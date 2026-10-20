"""使用本地 HTML 和真实浏览器控件验证答题；AI HTTP 响应为离线夹具。"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from activities import CourseActivities
from browser import ManualRequired
from gui_backend import Cancelled


class OfflineResponse:
    def __init__(self, answers):
        self.body = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps({'answers': answers})}}]}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def getcode(self):
        return 200

    def read(self, size):
        return self.body[:size]


class OfflineAI:
    def __init__(self, answers, before_reply=None):
        self.answers = answers
        self.before_reply = before_reply
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(json.loads(request.data))
        if self.before_reply:
            self.before_reply()
        return OfflineResponse(self.answers)


class QuizDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(channel='msedge', headless=True)
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_default_timeout(1500)
        self.adapter = SimpleNamespace(
            page=self.page, platform='uai', guard=lambda **kwargs: None,
            visible_frames=lambda: [self.page.main_frame], videos=lambda: [],
            start_attempts=set())
        self.course = CourseActivities(
            self.adapter, ai_base_url='https://ai.example/v1', ai_model='offline',
            ai_api_key='offline-key')
        self.answer = {'id': '0', 'selected': [0], 'confidence': .99}
        self.question = '''<fieldset><legend>Which number is even?</legend>
            <label><input type="radio" name="q1" value="two">Two</label>
            <label><input type="radio" name="q1" value="three">Three</label></fieldset>'''

    def tearDown(self):
        self.page.close()

    def load(self, questions=None, before='', after='', submitted='Submitted successfully'):
        self.page.set_content(f'''<main>{before}<form onsubmit="event.preventDefault();
            document.querySelector('#count').textContent = Number(document.querySelector('#count').textContent) + 1;
            document.querySelector('#result').textContent = '{submitted}';">
            {self.question if questions is None else questions}{after}
            <button type="submit">Submit answers</button></form>
            <output id="result"></output><output id="count">0</output></main>''')

    def assert_untouched(self, offline):
        self.assertEqual(offline.requests, [])
        self.assertEqual(self.page.locator('input:checked').count(), 0)
        self.assertEqual(self.page.locator('#count').inner_text(), '0')

    def test_uai_text_single_multiple_and_truefalse_submit_once(self):
        self.load(self.question + '''
            <fieldset><legend>Choose prime numbers.</legend>
              <label><input type="checkbox" name="q2">Two</label>
              <label><input type="checkbox" name="q2">Three</label>
              <label><input type="checkbox" name="q2">Four</label></fieldset>
            <fieldset><legend>One equals one.</legend>
              <label><input type="radio" name="q3">True</label>
              <label><input type="radio" name="q3">False</label></fieldset>''')
        offline = OfflineAI([self.answer,
            {'id': '1', 'selected': [0, 1], 'confidence': .99},
            {'id': '2', 'selected': [0], 'confidence': .99}])
        with patch('urllib.request.build_opener', return_value=offline):
            self.assertEqual(self.course.process(), 'working')
            self.assertEqual(self.course.process(), 'done')
            self.assertEqual(self.course.process(), 'done')
        self.assertEqual(self.page.locator('input:checked').count(), 4)
        self.assertEqual(self.page.locator('#count').inner_text(), '1')
        self.assertEqual(len(offline.requests), 1)
        sent = json.loads(offline.requests[0]['messages'][1]['content'])['questions']
        self.assertEqual([q['kind'] for q in sent], ['single', 'multiple', 'truefalse'])

    def test_mixed_unsupported_question_never_sends_or_submits_partial_exam(self):
        for answer_control in ('<input type="number">', '<select multiple><option>Fill in</option></select>',
                               '<div contenteditable="true">Type your answer</div>'):
            with self.subTest(control=answer_control):
                self.load(self.question + '<fieldset><legend>Complete the sentence.</legend>' +
                          answer_control + '</fieldset>')
                offline = OfflineAI([self.answer])
                with patch('urllib.request.build_opener', return_value=offline):
                    with self.assertRaises(ManualRequired):
                        self.course.process()
                self.assert_untouched(offline)

    def test_unrecognized_answer_control_outside_groups_blocks_partial_exam(self):
        self.load(after='<section><p>Explain why.</p><textarea></textarea></section>')
        offline = OfflineAI([self.answer])
        with patch('urllib.request.build_opener', return_value=offline):
            with self.assertRaisesRegex(ManualRequired, '完整|非选择'):
                self.course.process()
        self.assert_untouched(offline)

    def test_audio_outside_question_groups_requires_manual_work(self):
        self.load(before='<audio controls></audio>')
        offline = OfflineAI([self.answer])
        with patch('urllib.request.build_opener', return_value=offline):
            with self.assertRaisesRegex(ManualRequired, '媒体|听力|音频'):
                self.course.process()
        self.assert_untouched(offline)

    def test_missing_passage_instructions_outside_question_block_ai(self):
        self.load(before='<p>Read the passage and answer the following questions.</p>')
        offline = OfflineAI([self.answer])
        with patch('urllib.request.build_opener', return_value=offline):
            with self.assertRaisesRegex(ManualRequired, '材料|阅读'):
                self.course.process()
        self.assert_untouched(offline)

    def test_question_pagination_blocks_whole_exam_submission(self):
        self.load(after='<button type="button">Next question</button>')
        offline = OfflineAI([self.answer])
        with patch('urllib.request.build_opener', return_value=offline):
            with self.assertRaisesRegex(ManualRequired, '分页|完整'):
                self.course.process()
        self.assert_untouched(offline)

    def test_visible_question_picture_outside_group_is_not_omitted(self):
        self.load(before='<svg width="300" height="200" role="img" aria-label="Question diagram"><rect width="300" height="200"/></svg>')
        offline = OfflineAI([self.answer])
        with patch('urllib.request.build_opener', return_value=offline):
            with self.assertRaisesRegex(ManualRequired, '图片|媒体'):
                self.course.process()
        self.assert_untouched(offline)

    def test_text_popup_over_background_video_can_still_be_answered(self):
        self.load('<div role="dialog" aria-label="Knowledge check">' + self.question + '</div>',
                  before='<video width="320" height="200"></video>')
        offline = OfflineAI([self.answer])
        with patch('urllib.request.build_opener', return_value=offline):
            self.assertEqual(self.course.process(), 'working')
        self.assertEqual(self.page.locator('#count').inner_text(), '1')

    def test_header_logo_does_not_block_complete_text_quiz_in_main(self):
        self.load()
        self.page.locator('body').evaluate('''e => e.insertAdjacentHTML('afterbegin',
            '<header><svg width="120" height="30" role="img" aria-label="Site logo"></svg></header>')''')
        offline = OfflineAI([self.answer])
        with patch('urllib.request.build_opener', return_value=offline):
            self.assertEqual(self.course.process(), 'working')
        self.assertEqual(self.page.locator('#count').inner_text(), '1')

    def test_option_changed_during_ai_call_never_fills_or_submits(self):
        self.load()
        offline = OfflineAI([self.answer], before_reply=lambda:
            self.page.locator('label').first.evaluate("e => e.lastChild.textContent = 'Four'"))
        with patch('urllib.request.build_opener', return_value=offline):
            with self.assertRaisesRegex(ManualRequired, '变化'):
                self.course.process()
        self.assertEqual(len(offline.requests), 1)
        self.assertEqual(self.page.locator('input:checked').count(), 0)
        self.assertEqual(self.page.locator('#count').inner_text(), '0')

    def test_full_question_text_after_heading_is_sent(self):
        self.load(self.question.replace('</legend>', '</legend><p>Choose the smallest even number.</p>'))
        offline = OfflineAI([self.answer])
        with patch('urllib.request.build_opener', return_value=offline):
            self.course.process()
        sent = json.loads(offline.requests[0]['messages'][1]['content'])['questions'][0]
        self.assertIn('Choose the smallest even number.', sent['prompt'])

    def test_question_body_changed_during_ai_call_is_not_submitted(self):
        self.load(self.question.replace('</legend>', '</legend><p id="detail">Choose the smallest.</p>'))
        offline = OfflineAI([self.answer], before_reply=lambda:
            self.page.locator('#detail').evaluate("e => e.textContent = 'Choose the largest.'"))
        with patch('urllib.request.build_opener', return_value=offline):
            with self.assertRaisesRegex(ManualRequired, '变化'):
                self.course.process()
        self.assertEqual(self.page.locator('input:checked').count(), 0)
        self.assertEqual(self.page.locator('#count').inner_text(), '0')

    def test_svg_diagram_without_explicit_role_is_not_ignored(self):
        self.load(before='<svg width="300" height="200"><circle cx="80" cy="80" r="60"/></svg>')
        offline = OfflineAI([self.answer])
        with patch('urllib.request.build_opener', return_value=offline):
            with self.assertRaisesRegex(ManualRequired, '图片|媒体'):
                self.course.process()
        self.assert_untouched(offline)

    def test_embedded_frame_cannot_hide_part_of_the_quiz(self):
        self.load(after='<iframe srcdoc="&lt;textarea&gt;&lt;/textarea&gt;"></iframe>')
        offline = OfflineAI([self.answer])
        with patch('urllib.request.build_opener', return_value=offline):
            with self.assertRaisesRegex(ManualRequired, '嵌入|完整'):
                self.course.process()
        self.assert_untouched(offline)

    def test_answering_reveals_more_questions_without_partial_submission(self):
        self.load()
        self.page.locator('form').evaluate('''e => e.addEventListener('change', () => {
            e.insertAdjacentHTML('beforeend', '<fieldset><legend>Explain.</legend><textarea></textarea></fieldset>');
        }, {once: true})''')
        offline = OfflineAI([self.answer])
        with patch('urllib.request.build_opener', return_value=offline):
            with self.assertRaises(ManualRequired):
                self.course.process()
        self.assertEqual(self.page.locator('#count').inner_text(), '0')

    def test_shared_radio_name_cannot_leave_earlier_question_unanswered(self):
        self.load(self.question + self.question.replace('Which number is even?', 'Which is smaller?'))
        offline = OfflineAI([self.answer, {'id': '1', 'selected': [0], 'confidence': .99}])
        with patch('urllib.request.build_opener', return_value=offline):
            with self.assertRaises(ManualRequired):
                self.course.process()
        self.assertEqual(self.page.locator('#count').inner_text(), '0')

    def test_stop_during_ai_call_leaves_answers_untouched(self):
        self.load()
        stopped = [False]
        def check():
            if stopped[0]:
                raise Cancelled()
        self.course.control = SimpleNamespace(check=check, emit=lambda *args, **kwargs: None)
        offline = OfflineAI([self.answer], before_reply=lambda: stopped.__setitem__(0, True))
        with patch('urllib.request.build_opener', return_value=offline):
            with self.assertRaises(Cancelled):
                self.course.process()
        self.assertEqual(len(offline.requests), 1)
        self.assertEqual(self.page.locator('input:checked').count(), 0)
        self.assertEqual(self.page.locator('#count').inner_text(), '0')

    def test_low_confidence_last_answer_blocks_all_page_edits(self):
        self.load(self.question + self.question.replace('q1', 'q2'))
        offline = OfflineAI([self.answer, {'id': '1', 'selected': [0], 'confidence': .4}])
        with patch('urllib.request.build_opener', return_value=offline):
            with self.assertRaisesRegex(ManualRequired, '置信度'):
                self.course.process()
        self.assertEqual(len(offline.requests), 1)
        self.assertEqual(self.page.locator('input:checked').count(), 0)
        self.assertEqual(self.page.locator('#count').inner_text(), '0')


if __name__ == '__main__':
    unittest.main()
