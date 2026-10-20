"""根据已观察的 U校园 DOM 复现表格、内嵌空格和独立题目说明。"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from activities import CourseActivities
from browser import Adapter, ManualRequired
from quiz_dom import read_quiz, quiz_report


class StructuredQuizTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='msedge', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_default_timeout(1200)
        self.adapter = SimpleNamespace(page=self.page, platform='uai', guard=lambda **kw: None,
            visible_frames=lambda: [self.page.main_frame], videos=lambda: [], start_attempts=set())
        self.course = CourseActivities(self.adapter, ai_base_url='https://ai.example/v1',
            ai_model='offline', ai_api_key='offline-key')

    def tearDown(self):
        self.page.close()

    def load_uai(self, body, directions='Fill in the blanks with the correct form.'):
        self.page.set_content('''<section class="pc-main-container">
            <header><div class="pc-header-tasks-row"><div class="pc-task pc-header-task-activity">Task 1</div>
            <div class="pc-task pc-header-task-default">Task 2</div></div></header>
            <main class="pc-content-container"><div class="question-wrap"><div class="layout-container">
            <div class="layout-direction-container"><div class="abs-direction"><span>Directions:</span>
            <div class="component-htmlview content"><p>''' + directions + '''</p></div></div></div>
            <div class="fill-blank-reply"><div class="item"><div class="component-htmlview">'''
            + body + '''</div></div></div></div></div><output id="result"></output></main>
            <footer><a role="button" onclick="document.querySelector('#result').textContent=
            'Submitted successfully';this.dataset.clicks=+(this.dataset.clicks||0)+1;">提 交</a></footer></section>
            <aside><img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">
            <p>Reading comprehension / Listening / PRIVATE ACCOUNT</p><input aria-label="Search"></aside>''')

    def questions(self):
        item = self.course._detect()
        self.assertIsNotNone(item, '结构化答题区应被识别为小测')
        self.assertEqual(item.kind, 'quiz')
        return read_quiz(item)[0]

    def test_actual_table_rating_is_recognized_before_ai_configuration(self):
        rows = ''.join(f'<tr><td>{i}. I would feel uncomfortable without my smartphone.</td>'
            '<td><span class="fe-scoop"><span class="scoop-input-wrapper">'
            '<div class="comp-abs-input input-user-answer input-can-focus"><input></div>'
            '</span></span></td></tr>' for i in range(1, 11))
        self.load_uai('<table><tr><td>1 = Strongly disagree</td><td>7 = Strongly agree</td></tr></table>'
            '<table><tr><th>Statements</th><th>Your score</th></tr>' + rows + '</table>',
            'Rate each statement and write down the score that represents your opinion to make an assessment.')
        self.course.ai_api_key = ''
        item = self.course._detect()
        self.assertIsNotNone(item)
        self.assertEqual(len(item.root), 10)
        with patch('activities.solve_questions') as solve:
            with self.assertRaisesRegex(ManualRequired, r'10.*自评.*1.?7'):
                self.course.process()
        solve.assert_not_called()
        self.assertEqual(self.page.locator('main input').evaluate_all('es=>es.map(e=>e.value)'), [''] * 10)

    def test_table_context_and_multiblank_order_are_preserved(self):
        self.load_uai('''<table><tr><th>Sentence</th><th>Answer</th></tr>
            <tr><td>1. She goes to school.</td><td><input></td></tr>
            <tr><td colspan="2">For the next items, use the past tense.</td></tr>
            <tr><td>2. He <input> the map <input> across the table.</td><td>lay … out</td></tr></table>''')
        questions = self.questions()
        self.assertEqual([len(q.blanks) for q in questions], [1, 2])
        self.assertIn('[[q1_b0]] the map [[q1_b1]]', questions[1].prompt)
        self.assertIn('Sentence', questions[1].context)
        self.assertIn('use the past tense', questions[1].context)
        self.assertIn('correct form', questions[1].context)
        self.assertNotIn('PRIVATE ACCOUNT', str(questions))
        self.assertNotIn('Listening', str(questions))

    def test_hidden_table_instructions_are_not_omitted(self):
        self.load_uai('<table><tr hidden><th>Use past tense</th><th>Answer</th></tr>'
                      '<tr><td>1. She (go) to school.</td><td><input></td></tr></table>')
        with patch('activities.solve_questions') as solve:
            with self.assertRaisesRegex(ManualRequired, '材料|完整|折叠'):
                self.course.process()
        solve.assert_not_called()

    def test_changed_table_header_discards_ai_answer(self):
        self.load_uai('<table><tr><th>Present tense</th><th>Answer</th></tr>'
                      '<tr><td>1. She (go) to school.</td><td><input></td></tr></table>')
        def answer(questions, *_):
            self.page.locator('th').first.evaluate("e=>e.textContent='Past tense'")
            return [SimpleNamespace(id='0', selected=[], filled={'q0_b0': 'goes'})]
        with patch('activities.solve_questions', side_effect=answer):
            with self.assertRaisesRegex(ManualRequired, '变化'):
                self.course.process()
        self.assertEqual(self.page.locator('main input').input_value(), '')

    def test_uai_paragraph_and_list_inline_blanks_without_question_classes(self):
        self.load_uai('<p>He <span class="fe-scoop"><input></span> the map <input>.</p>'
                      '<ol><li>She <input> daily.</li></ol>')
        questions = self.questions()
        self.assertEqual([len(q.blanks) for q in questions], [2, 1])

    def test_generic_numbered_paragraph_and_table_need_no_platform_class(self):
        self.page.set_content('<main><p class="directions">Fill in the blanks.</p><p>1. She <input> daily.</p>'
            '<table><tr><th>Question</th><th>Answer</th></tr><tr><td>Capital of France</td>'
            '<td><input></td></tr></table></main>')
        self.assertEqual(len(self.questions()), 2)

    def test_nested_existing_groups_do_not_duplicate_table_blanks(self):
        self.load_uai('<fieldset><table><tr><td>One sentence.</td><td><p>1. She <input>.</p>'
            '</td></tr></table></fieldset>')
        self.assertEqual(len(self.questions()), 1)

    def test_unrelated_numbered_search_and_notes_are_not_quizzes(self):
        self.page.set_content('<aside><p>1. Search <input></p></aside>'
            '<nav><table><tr><td>Find course</td><td><input></td></tr></table></nav>'
            '<main><section>Notes <input></section></main>')
        self.assertIsNone(self.course._detect())

    def test_generic_account_table_is_not_mistaken_for_a_question(self):
        self.page.set_content('<main><form><table><tr><td>Your name</td><td><input></td></tr>'
            '<tr><td>Email address</td><td><input type="email"></td></tr></table></form></main>')
        self.assertIsNone(self.course._detect())

    def test_numbered_account_form_is_never_filled_or_submitted(self):
        for body in ('<p>1. Full name <input value="Existing Name"></p>'
                     '<p>2. Mailing address <input value="Existing Address"></p>',
                     '<table><tr><td>1. Full name</td><td><input value="Existing Name"></td></tr>'
                     '<tr><td>2. Mailing address</td><td><input value="Existing Address"></td></tr></table>'):
            with self.subTest(body=body):
                self.page.set_content('<main><form><h1>Account profile</h1>' + body +
                    '<button type="button" onclick="this.dataset.clicked=1">Submit</button></form></main>')
                with patch('activities.solve_questions') as solve:
                    self.assertIsNone(self.course.process())
                solve.assert_not_called()
                self.assertEqual(self.page.locator('input').evaluate_all('es=>es.map(e=>e.value)'),
                                 ['Existing Name', 'Existing Address'])
                self.assertIsNone(self.page.locator('button').get_attribute('data-clicked'))

    def test_table_caption_is_part_of_instructions_and_change_validation(self):
        self.load_uai('<table><caption>Write the past tense of each verb.</caption>'
                      '<tr><th>Verb</th><th>Answer</th></tr><tr><td>go</td><td><input></td></tr></table>',
                      'Complete the table.')
        self.assertIn('Write the past tense', self.questions()[0].context)
        def answer(questions, *_):
            self.page.locator('caption').evaluate("e=>e.textContent='Write the present participle.'")
            return [SimpleNamespace(id='0', selected=[], filled={'q0_b0': 'went'})]
        with patch('activities.solve_questions', side_effect=answer):
            with self.assertRaisesRegex(ManualRequired, '变化'):
                self.course.process()
        self.assertEqual(self.page.locator('main input').input_value(), '')

    def test_report_gives_counts_and_reason_without_answer_values(self):
        self.load_uai('<p>1. First <input value="PRIVATE ANSWER"></p><p>2. Second <input></p>')
        report = quiz_report(self.page.main_frame)
        self.assertEqual((report['questions'], report['blanks'], report['status']), (2, 2, 'supported'))
        self.assertNotIn('PRIVATE', str(report))
        self.load_uai('<table><tr><td>1. Your feelings</td><td><input></td></tr></table>',
                      '1 = Strongly disagree; 7 = Strongly agree.')
        report = quiz_report(self.page.main_frame)
        self.assertEqual(report['status'], 'manual')
        self.assertIn('1—7', report['message'])

    def test_page_inspection_includes_question_diagnosis(self):
        self.load_uai('<p>1. First <input></p><p>2. Second <input></p>')
        report = Adapter(self.page, 'uai').inspect()
        self.assertEqual(report['frames'][0]['quiz']['blanks'], 2)

    def test_new_task_does_not_inherit_previous_manual_submission(self):
        self.load_uai('<p>1. Answer <input>.</p>')
        self.course.quiz_submit = False
        answer = SimpleNamespace(id='0', selected=[], filled={'q0_b0': 'word'})
        with patch('activities.solve_questions', return_value=[answer]) as solve:
            with self.assertRaisesRegex(ManualRequired, '手动提交'):
                self.course.process()
            old = self.course.pending_quiz
            self.page.locator('.pc-task').evaluate_all('es=>{es[0].className="pc-task";'
                'es[1].className="pc-task pc-header-task-activity";}')
            self.page.locator('#result').evaluate("e=>e.textContent='Submitted successfully'")
            with self.assertRaisesRegex(ManualRequired, '切换'):
                self.course.process()
            self.assertIsNone(self.course.pending_quiz)
            self.assertNotIn(self.course._detect().key, self.course.completed)
            self.assertIn(old.key, self.course.completed)
            solve.assert_called_once()

    def test_hidden_table_blank_and_unknown_control_prevent_partial_submission(self):
        for extra in ('<tr hidden><td>Second question</td><td><input></td></tr>',
                      '<tr><td>Second question</td><td><div role="textbox" contenteditable="true"></div></td></tr>'):
            with self.subTest(extra=extra):
                self.load_uai('<table><tr><td>First question</td><td><input></td></tr>' + extra + '</table>')
                self.assertIsNotNone(self.course._detect())
                with patch('activities.solve_questions') as solve:
                    with self.assertRaisesRegex(ManualRequired, '隐藏|控件|识别'):
                        self.course.process()
                solve.assert_not_called()

    def test_ordinary_rating_words_in_factual_question_do_not_become_personal(self):
        self.load_uai('<p>1. Translate "assessment". <input></p>')
        self.assertEqual(self.questions()[0].kind, 'fill')

    def test_explicit_directions_are_kept_even_without_material_keywords(self):
        self.load_uai('<p>1. Answer <input>.</p>', 'Write the antonym of each adjective.')
        self.assertIn('antonym', self.questions()[0].context)

    def test_chinese_spaced_submit_works_and_fills_stay_bound_to_main(self):
        self.load_uai('<table><tr><td>1. She (go) to school.</td><td><input></td></tr></table>')
        answer = SimpleNamespace(id='0', selected=[], filled={'q0_b0': 'goes'})
        with patch('activities.solve_questions', return_value=[answer]):
            self.assertEqual(self.course.process(), 'working')
            self.assertEqual(self.course.process(), 'done')
        self.assertEqual(self.page.locator('main input').input_value(), 'goes')
        self.assertEqual(self.page.locator('aside input').input_value(), '')
        self.assertEqual(self.page.locator('footer a').get_attribute('data-clicks'), '1')

    def test_same_url_task_switch_discards_inflight_answers(self):
        self.load_uai('<p>1. Answer <input>.</p>')
        def switch(questions, *_):
            self.page.locator('.pc-task').evaluate_all('es=>{es[0].className="pc-task";'
                'es[1].className="pc-task pc-header-task-activity";}')
            return [SimpleNamespace(id='0', selected=[], filled={'q0_b0': 'word'})]
        with patch('activities.solve_questions', side_effect=switch):
            with self.assertRaisesRegex(ManualRequired, '变化'):
                self.course.process()
        self.assertEqual(self.page.locator('main input').input_value(), '')


if __name__ == '__main__':
    unittest.main()
