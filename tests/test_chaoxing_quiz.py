"""超星三层课件、特殊字体与隐藏验证弹窗的本地回归。"""
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from activities import CourseActivities
from browser import Adapter, ManualRequired
from quiz_ai import QuizAnswer


class ChaoxingQuizTests(unittest.TestCase):
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
        self.page.route('https://mooc1.chaoxing.com/**', lambda route: route.fulfill(
            body='<body><iframe style="width:100%;height:900px"></iframe></body>'))
        self.page.goto('https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId=1')
        for frame in (self.page.frames[1],):
            frame.set_content('<iframe style="width:100%;height:850px"></iframe>')
        self.page.frames[2].set_content('<iframe style="width:100%;height:800px"></iframe>')
        self.frame = self.page.frames[3]
        self.adapter = Adapter(self.page, 'chaoxing')
        self.course = CourseActivities(self.adapter, ai_base_url='https://ai.example/v1',
                                       ai_model='offline', ai_api_key='offline-key')

    def tearDown(self):
        self.page.close()

    def load(self, font=False, hidden_question=False):
        font_class = 'font-cxsecret' if font else ''
        questions = ''
        for i in range(3):
            questions += f'''<div class="singleQuesId"><div class="TiMu newTiMu">
            <div class="Zy_TItle"><i>{i+1}</i><div class="fontLabel {font_class}">Q{i}: 选择数字二</div></div>
            <ul><li role="radio" aria-checked="false" class="{font_class}"
              onclick="for(const n of this.parentNode.children)n.setAttribute('aria-checked',String(n===this))">
              <label><span class="num_option">A</span></label><a class="after">二</a></li>
              <li role="radio" aria-checked="false" class="{font_class}"
              onclick="for(const n of this.parentNode.children)n.setAttribute('aria-checked',String(n===this))">
              <label><span class="num_option">B</span></label><a class="after">三</a></li></ul></div></div>'''
        extra = '<div class="TiMu" hidden><input></div>' if hidden_question else ''
        self.frame.set_content(f'''<style>.font-cxsecret{{font-family:font-cxsecret}}</style>
            <div class="RightCon newTestCon"><form><div class="ZyBottom ans-cc">{questions}{extra}</div></form>
            <div class="AlertCon02" style="display:none"><input type="text"></div>
            <button onclick="this.dataset.count=+(this.dataset.count||0)+1">提交</button></div>''')

    def test_three_layer_quiz_ignores_unrelated_hidden_captcha(self):
        self.load()
        item = self.course._detect()
        questions, _ = self.course._read_quiz(item)
        self.assertEqual(len(questions), 3)
        self.assertEqual(questions[0].options, ['A二', 'B三'])

    def test_hidden_question_in_same_quiz_is_still_blocked(self):
        self.load(hidden_question=True)
        with self.assertRaisesRegex(ManualRequired, '隐藏|折叠'):
            self.course._read_quiz(self.course._detect())

    def test_special_font_is_read_locally_before_ai_and_submit_once(self):
        self.load(font=True)
        def text(node):
            raw = node.inner_text()
            return '识别题干：选择数字二' if 'Q' in raw else raw

        seen = []
        def solve(questions, *args, **kwargs):
            seen.extend(questions)
            return [QuizAnswer(str(i), [0], .99) for i in range(3)]

        with patch('quiz_dom.ocr_text', side_effect=text) as ocr, \
             patch('activities.solve_questions', side_effect=solve):
            self.assertEqual(self.course.process(), 'working')
            self.assertEqual(self.course.process(), 'working')
        self.assertEqual(len(seen), 3)
        self.assertTrue(all('识别题干' in q.prompt and 'Q' not in q.prompt for q in seen))
        self.assertEqual(seen[0].options, ['二', '三'])
        self.assertGreaterEqual(ocr.call_count, 9)
        self.assertEqual(self.frame.locator('button').get_attribute('data-count'), '1')

    def test_ocr_failure_does_not_send_garbled_text_or_submit(self):
        self.load(font=True)
        with patch('quiz_dom.ocr_text', side_effect=ManualRequired('本机文字识别不可用')), \
             patch('activities.solve_questions') as solve:
            with self.assertRaisesRegex(ManualRequired, '文字识别'):
                self.course.process()
        solve.assert_not_called()
        self.assertIsNone(self.frame.locator('button').get_attribute('data-count'))

    def test_observed_option_text_is_stable_when_aria_label_changes(self):
        from quiz_dom import option_text
        self.load()
        option = self.frame.locator('[role=radio]').first
        option.evaluate("e=>e.setAttribute('aria-label','A 二选择')")
        before = option_text(option)
        option.evaluate("e=>e.setAttribute('aria-label','A 二已选择')")
        self.assertEqual(option_text(option), before)

    def test_observed_circle_class_is_verified_without_aria_checked(self):
        self.load()
        self.frame.locator('[role=radio]').evaluate_all("""nodes=>nodes.forEach(e=>{
            e.removeAttribute('aria-checked');
            e.onclick=()=>{
                for(const n of e.parentNode.children){
                    n.querySelector('.num_option').classList.toggle('check_answer',n===e);
                    n.setAttribute('aria-label',n.innerText+(n===e?'已选择':'选择'));
                }
            };
        })""")
        with patch('activities.solve_questions', return_value=[
                QuizAnswer(str(i), [1], .99) for i in range(3)]):
            self.assertEqual(self.course.process(), 'working')
        self.assertEqual(self.frame.locator('.check_answer').all_inner_texts(), ['B'] * 3)
        self.assertEqual(self.frame.locator('button').get_attribute('data-count'), '1')

    def test_unknown_option_selection_state_stops_before_submit(self):
        self.load()
        self.frame.locator('[role=radio]').evaluate_all("""nodes=>nodes.forEach(e=>{
            e.removeAttribute('aria-checked');
            e.querySelector('.num_option').className='unknown-circle';
        })""")
        with patch('activities.solve_questions', return_value=[
                QuizAnswer(str(i), [0], .99) for i in range(3)]):
            with self.assertRaisesRegex(ManualRequired, '选中状态'):
                self.course.process()
        self.assertIsNone(self.frame.locator('button').get_attribute('data-count'))

    def test_result_outside_form_in_same_quiz_is_confirmed(self):
        self.load()
        with patch('activities.solve_questions', return_value=[
                QuizAnswer(str(i), [0], .99) for i in range(3)]) as solve:
            self.assertEqual(self.course.process(), 'working')
            self.frame.locator('.RightCon').evaluate(
                "e=>e.insertAdjacentHTML('beforeend','<output>提交成功</output>')")
            self.assertEqual(self.course.process(), 'done')
        solve.assert_called_once()
        self.assertEqual(self.frame.locator('button').get_attribute('data-count'), '1')

    def test_unrelated_result_cannot_confirm_quiz(self):
        self.load()
        with patch('activities.solve_questions', return_value=[
                QuizAnswer(str(i), [0], .99) for i in range(3)]):
            self.assertEqual(self.course.process(), 'working')
            self.frame.locator('body').evaluate("""e=>e.insertAdjacentHTML('beforeend',
                '<div class="RightCon newTestCon"><output>提交成功</output></div>')""")
            self.frame.locator('.RightCon').first.evaluate("""e=>e.insertAdjacentHTML('beforeend',
                '<aside><output>提交成功</output></aside>')""")
            self.assertEqual(self.course.process(), 'working')

    def test_result_survives_question_form_removal(self):
        self.load()
        with patch('activities.solve_questions', return_value=[
                QuizAnswer(str(i), [0], .99) for i in range(3)]):
            self.assertEqual(self.course.process(), 'working')
            self.frame.locator('form').evaluate('e=>e.remove()')
            self.frame.locator('.RightCon').evaluate(
                "e=>e.insertAdjacentHTML('beforeend','<output>提交成功</output>')")
            self.assertEqual(self.course.process(), 'done')
        self.assertEqual(self.frame.locator('button').get_attribute('data-count'), '1')

    def test_clipped_ocr_title_stops_before_ai(self):
        self.load(font=True)
        self.frame.locator('.Zy_TItle').first.evaluate("""e=>{
            e.style.cssText='height:55px;overflow:auto';
            e.querySelector('.fontLabel').style.cssText='height:150px;line-height:50px';
            e.querySelector('.fontLabel').innerHTML='第一行条件<br>第二行条件<br>第三行条件';
        }""")
        with patch('quiz_ocr._recognize', return_value='不完整的题干') as recognize, \
             patch('activities.solve_questions') as solve:
            with self.assertRaisesRegex(ManualRequired, '裁剪'):
                self.course.process()
        recognize.assert_not_called()
        solve.assert_not_called()
        self.assertIsNone(self.frame.locator('button').get_attribute('data-count'))


class LocalOCRTests(unittest.TestCase):
    def test_windows_ocr_reads_rendered_chinese_without_network(self):
        from quiz_ocr import read_text
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True)
            page = browser.new_page()
            page.set_content('<p style="font:28px Microsoft YaHei;width:600px">语音识别（ASR）</p>')
            with patch('urllib.request.urlopen', side_effect=AssertionError('network not allowed')):
                result = read_text(page.locator('p'))
            self.assertIn('语音识别', result)
            self.assertIn('ASR', result)
            browser.close()

    def test_windows_ocr_reads_single_character_choices(self):
        from quiz_ocr import read_text
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True)
            page = browser.new_page()
            for size in (16, 20):
                for text in ('对', '错'):
                    with self.subTest(size=size, text=text):
                        page.set_content(
                            f'<span style="font:{size}px Microsoft YaHei">{text}</span>')
                        self.assertEqual(read_text(page.locator('span')), text)
            browser.close()

    def test_ambiguous_short_character_is_correct_or_requires_review(self):
        from quiz_ocr import read_text
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True)
            page = browser.new_page()
            for text in ('二', '三'):
                for size in (16, 20):
                    with self.subTest(text=text, size=size):
                        page.set_content(f'<span style="font:{size}px Microsoft YaHei">{text}</span>')
                        try:
                            result = read_text(page.locator('span'))
                        except ManualRequired as error:
                            self.assertIn('文字识别', str(error))
                        else:
                            self.assertEqual(result, text)
            browser.close()

    def test_scrollable_text_inside_target_stops_before_ocr(self):
        from quiz_ocr import read_text
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True)
            page = browser.new_page()
            for inner in (False, True):
                with self.subTest(inner=inner):
                    text = ('<div style="height:55px;overflow:auto;font:28px Microsoft YaHei;'
                            'line-height:50px">第一行条件<br>第二行条件<br>第三行条件</div>')
                    page.set_content('<main>' + text + '</main>')
                    node = page.locator('main' if inner else 'div')
                    with patch('quiz_ocr._recognize', return_value='第一行条件') as recognize:
                        with self.assertRaisesRegex(ManualRequired, '裁剪'):
                            read_text(node)
                    recognize.assert_not_called()
            browser.close()

    def test_iframe_clipped_by_parent_stops_before_ocr(self):
        from quiz_ocr import read_text
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True)
            page = browser.new_page()
            page.set_content(
                '<div style="height:40px;overflow:hidden"><iframe height="200"></iframe></div>')
            frame = page.frames[1]
            frame.set_content(
                '<p style="margin:0;font:22px Microsoft YaHei;line-height:40px">'
                '第一行条件<br>第二行条件<br>第三行条件</p>')
            with patch('quiz_ocr._recognize', return_value='不完整的题干') as recognize:
                with self.assertRaisesRegex(ManualRequired, '裁剪'):
                    read_text(frame.locator('p'))
            recognize.assert_not_called()
            browser.close()


if __name__ == '__main__':
    unittest.main()
