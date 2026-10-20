"""真实本地 HTML 验证填空、共享材料、重绘及人工提交；无课程网络访问。"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from activities import CourseActivities
from browser import ManualRequired


class FillDOMTests(unittest.TestCase):
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
        self.seen = []

    def tearDown(self):
        self.page.close()

    def load(self, body, before='', after=''):
        self.page.set_content('''<header>PRIVATE ACCOUNT NAME</header><main>''' + before + '''
            <form onsubmit="event.preventDefault(); document.querySelector('#count').textContent =
            +document.querySelector('#count').textContent+1; document.querySelector('#result').textContent=
            'Submitted successfully';">''' + body + after + '''<button>Submit answers</button>
            </form><output id="count">0</output><output id="result"></output></main>''')

    def solve(self, values, callback=None):
        def answer(questions, *_):
            self.seen.append(questions)
            if callback:
                callback()
            result = []
            for question in questions:
                filled = {blank.id: values[question.id][index]
                          for index, blank in enumerate(question.blanks)}
                result.append(SimpleNamespace(id=question.id, selected=[] if filled else [0],
                    confidence=.99, filled=filled, sources={}))
            return result
        return patch('activities.solve_questions', side_effect=answer)

    def test_sentence_blanks_keep_positions_and_declared_limits(self):
        self.load('<fieldset><legend>Complete.</legend>He <input maxlength="12"> the map '
                  '<input maxlength="8"> on the table.</fieldset>')
        with self.solve({'0': ['laid', 'out']}):
            self.assertEqual(self.course.process(), 'working')
            self.assertEqual(self.course.process(), 'done')
        question = self.seen[0][0]
        self.assertEqual(question.kind, 'fill')
        self.assertIn('He [[q0_b0]] the map [[q0_b1]] on the table.', question.prompt)
        self.assertEqual([b.max_length for b in question.blanks], [12, 8])
        self.assertEqual(self.page.locator('input').evaluate_all('es=>es.map(e=>e.value)'), ['laid', 'out'])
        self.assertEqual(self.page.locator('#count').inner_text(), '1')

    def test_mixed_choice_textarea_and_select_all_verified(self):
        self.load('''<fieldset><legend>Choose.</legend><label><input type=radio name=q>A</label>
            <label><input type=radio name=q>B</label></fieldset>
            <fieldset><legend>Translate hello.</legend><textarea></textarea></fieldset>
            <fieldset><legend>She <select required><option value="">Choose</option>
            <option value="a">runs</option><option value="b">run</option></select> daily.</legend></fieldset>''')
        with self.solve({'1': ['你好'], '2': ['runs']}):
            self.course.process()
        self.assertEqual([q.kind for q in self.seen[0]], ['single', 'fill', 'fill'])
        self.assertEqual(self.seen[0][2].blanks[0].options, ['runs', 'run'])
        self.assertEqual(self.page.locator('select').input_value(), 'a')
        self.assertEqual(self.page.locator('textarea').input_value(), '你好')
        self.assertEqual(self.page.locator('#count').inner_text(), '1')

    def test_shared_phrase_bank_instructions_and_article_are_sent_without_account(self):
        self.load('<fieldset><legend>According to the passage, finish.</legend>'
                  'He <input> the map <input>.</fieldset>', before='''
            <p class="instructions">Use each phrase only once; change its form if necessary.</p>
            <div class="word-bank"><span>lay … out</span><span>pick … up</span></div>
            <article class="reading-passage">Tom laid his map across a table before leaving home.</article>''')
        with self.solve({'0': ['laid', 'out']}):
            self.course.process()
        question = self.seen[0][0]
        self.assertEqual(question.word_bank, ['lay … out', 'pick … up'])
        self.assertEqual(question.bank_reuse, 'once')
        self.assertIn('Tom laid his map', question.context)
        self.assertIn('change its form', question.context)
        self.assertNotIn('PRIVATE ACCOUNT', str(question))

    def test_bank_phrase_keeps_nested_spans_together(self):
        self.load('<fieldset>He <input> the map.</fieldset>', before='''
            <div class="word-bank"><span><span>lay</span> … <span>out</span></span>
            <span>pick … up</span></div>''')
        with self.solve({'0': ['laid out']}):
            self.course.process()
        self.assertEqual(self.seen[0][0].word_bank, ['lay … out', 'pick … up'])

    def test_bank_only_attached_to_its_semantic_question_section(self):
        self.load('''<section><div class="word-bank"><span>run</span><span>walk</span></div>
            <fieldset>He can <input>.</fieldset></section>
            <section><fieldset>Translate hello. <textarea></textarea></fieldset></section>''')
        with self.solve({'0': ['run'], '1': ['你好']}):
            self.course.process()
        self.assertEqual(self.seen[0][0].word_bank, ['run', 'walk'])
        self.assertEqual(self.seen[0][1].word_bank, [])

    def test_bank_in_separate_section_with_ambiguous_question_groups_is_manual(self):
        self.load('''<section><fieldset>He can <input>.</fieldset></section>
            <section><fieldset>Translate hello. <textarea></textarea></fieldset></section>''',
            before='<section><div class="word-bank"><span>run</span><span>walk</span></div></section>')
        with patch('activities.solve_questions') as solve:
            with self.assertRaisesRegex(ManualRequired, '词库|对应'):
                self.course.process()
        solve.assert_not_called()

    def test_explicit_reusable_bank_and_unspecified_rule_are_distinguished(self):
        for instruction, expected in [('Each phrase can be used more than once.', 'allowed'),
                                      ('Choose a phrase from the word bank.', 'unspecified')]:
            with self.subTest(instruction=instruction):
                self.course = CourseActivities(self.adapter, ai_base_url='https://ai.example/v1',
                    ai_model='offline', ai_api_key='offline-key')
                self.load('<fieldset>He can <input>.</fieldset>', before='<p class="instructions">'
                    + instruction + '</p><div class="word-bank"><span>run</span><span>walk</span></div>')
                with self.solve({'0': ['run']}):
                    self.course.process()
                self.assertEqual(self.seen[-1][0].bank_reuse, expected)

    def test_reading_material_with_hidden_overflow_blocks_ai(self):
        self.load('<fieldset>According to the passage <input>.</fieldset>', before='''
            <article class="passage" style="height:10px;overflow:hidden">First line<br>
            Essential second line<br>Final line</article>''')
        with patch('activities.solve_questions') as solve:
            with self.assertRaisesRegex(ManualRequired, '截断|完整'):
                self.course.process()
        solve.assert_not_called()

    def test_hidden_question_in_submit_scope_blocks_partial_exam(self):
        self.load('<fieldset>Visible answer <input></fieldset>'
                  '<fieldset hidden>Required second answer <input></fieldset>')
        with patch('activities.solve_questions') as solve:
            with self.assertRaisesRegex(ManualRequired, '隐藏|折叠|完整'):
                self.course.process()
        solve.assert_not_called()
        self.assertEqual(self.page.locator('#count').inner_text(), '0')

    def test_hidden_blank_within_visible_question_blocks_partial_answer(self):
        self.load('<fieldset>First <input><span hidden>second <input></span></fieldset>')
        with patch('activities.solve_questions') as solve:
            with self.assertRaisesRegex(ManualRequired, '隐藏|完整'):
                self.course.process()
        solve.assert_not_called()

    def test_hidden_material_body_blocks_partial_passage(self):
        self.load('<fieldset>According to the passage <input>.</fieldset>', before='''
            <article class="passage">First part<div hidden>Essential answer is here.</div></article>''')
        with patch('activities.solve_questions') as solve:
            with self.assertRaisesRegex(ManualRequired, '隐藏|折叠|完整'):
                self.course.process()
        solve.assert_not_called()

    def test_hidden_native_choices_with_visible_labels_and_auxiliary_markup_remain_supported(self):
        self.load('''<fieldset><legend>According to the passage, select the word.</legend>
            <label><input style="display:none" type="radio" name="q">run</label>
            <label><input style="display:none" type="radio" name="q">walk</label></fieldset>''',
            before='''<article class="passage">He can run.
            <span hidden aria-hidden="true">Decorative helper</span><script type="application/json">{}</script>
            </article>''')
        with self.solve({}):
            self.assertEqual(self.course.process(), 'working')
        self.assertEqual(self.page.locator('input:checked').count(), 1)
        self.assertEqual(self.page.locator('#count').inner_text(), '1')

    def test_personal_experience_requires_actual_user_content(self):
        self.load('<fieldset>Describe a memorable event from your own life.<textarea></textarea></fieldset>')
        with patch('activities.solve_questions') as solve:
            with self.assertRaisesRegex(ManualRequired, '个人|自评'):
                self.course.process()
        solve.assert_not_called()

    def test_plain_past_tense_instruction_is_sent(self):
        self.load('<fieldset>He <input> to school. (go)</fieldset>', before='<p>Use the past tense.</p>')
        with self.solve({'0': ['went']}):
            self.course.process()
        self.assertIn('Use the past tense.', self.seen[0][0].context)

    def test_unrecognized_instruction_is_not_silently_omitted(self):
        self.load('<fieldset>Answer <input>.</fieldset>', before='<p>Instructions: use the secret convention above.</p>')
        with patch('activities.solve_questions') as solve:
            with self.assertRaisesRegex(ManualRequired, '说明|规则'):
                self.course.process()
        solve.assert_not_called()

    def test_missing_shared_material_stops_before_ai(self):
        for instruction in ('Read the passage and fill in the blanks.',
                            'Choose phrases from the word bank.'):
            with self.subTest(instruction=instruction):
                self.load('<fieldset>Answer <input></fieldset>', before='<p>'+instruction+'</p>')
                with patch('activities.solve_questions') as solve:
                    with self.assertRaisesRegex(ManualRequired, '材料|词库'):
                        self.course.process()
                solve.assert_not_called()

    def test_hidden_passage_and_unrecognized_answer_control_stop_before_ai(self):
        for extra in ('<article class="passage" hidden>Unread material</article>',
                      '<section>Extra answer <input></section>',
                      '<div draggable="true">Drag this word</div>'):
            with self.subTest(extra=extra):
                self.load('<fieldset>Answer <input></fieldset>', after=extra)
                with patch('activities.solve_questions') as solve:
                    with self.assertRaises(ManualRequired):
                        self.course.process()
                solve.assert_not_called()

    def test_personal_rating_is_not_sent_for_invented_answers(self):
        self.load('<fieldset>I would be scared if my smartphone ran out of battery. <input></fieldset>',
                  before='<p>1 = Strongly disagree; 7 = Strongly agree. Your score</p>')
        with patch('activities.solve_questions') as solve:
            with self.assertRaisesRegex(ManualRequired, '个人|自评'):
                self.course.process()
        solve.assert_not_called()

    def test_material_changes_while_ai_running_discard_answers(self):
        self.load('<fieldset>Fill <input></fieldset>',
                  before='<div class="word-bank"><span>run</span><span>walk</span></div>')
        with self.solve({'0': ['run']}, callback=lambda: self.page.locator('.word-bank span').first
                        .evaluate("e=>e.textContent='jump'")):
            with self.assertRaisesRegex(ManualRequired, '变化'):
                self.course.process()
        self.assertEqual(self.page.locator('input').input_value(), '')
        self.assertEqual(self.page.locator('#count').inner_text(), '0')

    def test_framework_clearing_input_blocks_submission(self):
        self.load('<fieldset>Fill <input oninput="this.value=\'\'"></fieldset>')
        with self.solve({'0': ['run']}):
            with self.assertRaisesRegex(ManualRequired, '保留|填写'):
                self.course.process()
        self.assertEqual(self.page.locator('#count').inner_text(), '0')

    def test_browser_pattern_invalid_value_blocks_submission(self):
        self.load('<fieldset>Fill <input pattern="[A-Z]+"></fieldset>')
        with self.solve({'0': ['run']}):
            with self.assertRaisesRegex(ManualRequired, '格式|有效|约束'):
                self.course.process()
        self.assertEqual(self.page.locator('#count').inner_text(), '0')

    def test_repainted_input_is_read_back_and_not_filled_into_detached_node(self):
        self.load('<fieldset>Fill <input oninput="this.outerHTML=\'<input value=&quot;kept&quot;>\'"></fieldset>')
        with self.solve({'0': ['wrong']}):
            with self.assertRaises(ManualRequired):
                self.course.process()
        self.assertEqual(self.page.locator('#count').inner_text(), '0')

    def test_repaint_preserving_value_can_submit_and_does_not_change_question_identity(self):
        self.load('<fieldset>Fill <textarea oninput="this.outerHTML=\'<textarea>kept</textarea>\'"></textarea></fieldset>')
        with self.solve({'0': ['kept']}):
            self.assertEqual(self.course.process(), 'working')
            self.assertEqual(self.course.process(), 'done')
        self.assertEqual(self.page.locator('#count').inner_text(), '1')

    def test_editing_later_blank_cannot_clear_earlier_answer_before_submit(self):
        self.load('''<fieldset>First <input id="first"> second
            <input oninput="document.getElementById('first').value=''"></fieldset>''')
        with self.solve({'0': ['run', 'walk']}):
            with self.assertRaisesRegex(ManualRequired, '保留|填写'):
                self.course.process()
        self.assertEqual(self.page.locator('#count').inner_text(), '0')

    def test_manual_submit_waits_without_repeated_ai_and_resumes_after_success(self):
        self.course = CourseActivities(self.adapter, ai_base_url='https://ai.example/v1',
            ai_model='offline', ai_api_key='offline-key', quiz_submit=False)
        self.load('<fieldset>Fill <input></fieldset>')
        with self.solve({'0': ['run']}) as solve:
            with self.assertRaisesRegex(ManualRequired, '手动提交|检查'):
                self.course.process()
            with self.assertRaisesRegex(ManualRequired, '手动提交|检查'):
                self.course.process()
            self.assertEqual(self.page.locator('#count').inner_text(), '0')
            self.page.get_by_role('button', name='Submit answers').click()
            self.assertEqual(self.course.process(), 'done')
            self.assertEqual(solve.call_count, 1)

    def test_failure_text_is_never_submission_success(self):
        for result in ('Your answers were not submitted successfully. Please try again.',
                       '提交失败，未提交成功，请重试。'):
            with self.subTest(result=result):
                self.course = CourseActivities(self.adapter, ai_base_url='https://ai.example/v1',
                    ai_model='offline', ai_api_key='offline-key')
                self.load('<fieldset>Fill <input></fieldset>')
                self.page.locator('form').evaluate('''(form,result)=>form.onsubmit=e=>{
                    e.preventDefault(); document.querySelector('#result').innerHTML=result;
                }''', result)
                with self.solve({'0': ['run']}):
                    self.assertEqual(self.course.process(), 'working')
                    self.assertEqual(self.course.process(), 'working')

    def test_unrelated_status_cannot_finish_waiting_quiz(self):
        for unrelated in ('<aside>Other assignment submitted successfully</aside>',
                          '<section><output>Submitted successfully</output></section>'):
            with self.subTest(unrelated=unrelated):
                self.course = CourseActivities(self.adapter, ai_base_url='https://ai.example/v1',
                    ai_model='offline', ai_api_key='offline-key', quiz_submit=False)
                self.load('<fieldset>Fill <input></fieldset>')
                with self.solve({'0': ['run']}) as solve:
                    with self.assertRaises(ManualRequired):
                        self.course.process()
                    self.page.locator('main').evaluate('(e,html)=>e.insertAdjacentHTML("beforeend",html)', unrelated)
                    with self.assertRaises(ManualRequired):
                        self.course.process()
                    self.assertEqual(solve.call_count, 1)
                self.assertEqual(self.page.locator('#count').inner_text(), '0')

    def test_ancestor_cannot_reintroduce_excluded_success_text_after_form_disappears(self):
        for unrelated in ('<aside>Submitted successfully</aside>',
                          '<section><output>Submitted successfully</output></section>'):
            with self.subTest(unrelated=unrelated):
                self.course = CourseActivities(self.adapter, ai_base_url='https://ai.example/v1',
                    ai_model='offline', ai_api_key='offline-key', quiz_submit=False)
                self.load('<fieldset>Fill <input></fieldset>')
                with self.solve({'0': ['run']}) as solve:
                    with self.assertRaises(ManualRequired):
                        self.course.process()
                    self.page.locator('main').evaluate('''(e,html)=>{
                        e.replaceChildren(); e.insertAdjacentHTML('beforeend',html);
                    }''', unrelated)
                    with self.assertRaises(ManualRequired):
                        self.course.process()
                    self.assertEqual(solve.call_count, 1)

    def test_disappeared_questions_keep_confirmed_result_scope(self):
        self.load('<fieldset>Fill <input></fieldset>')
        self.page.locator('form').evaluate('''form=>form.onsubmit=e=>{
            e.preventDefault(); form.remove(); document.querySelector('#result').textContent='Submitted successfully';
        }''')
        with self.solve({'0': ['run']}):
            self.assertEqual(self.course.process(), 'working')
            self.assertEqual(self.course.process(), 'done')

    def test_replaced_main_requires_result_confirmation_not_stale_scope(self):
        self.load('<fieldset>Fill <input></fieldset>')
        self.page.locator('form').evaluate('''form=>form.onsubmit=e=>{
            e.preventDefault(); document.querySelector('main').outerHTML='<main>Submitted successfully</main>';
        }''')
        with self.solve({'0': ['run']}):
            self.assertEqual(self.course.process(), 'working')
            self.assertEqual(self.course.process(), 'working')


if __name__ == '__main__':
    unittest.main()
