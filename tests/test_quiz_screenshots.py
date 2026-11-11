"""截图答题：本地页面真实截图和控件操作，远程模型使用离线 HTTP 响应。"""
import base64
import json
import struct
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from activities import CourseActivities
from browser import ManualRequired
from quiz_ai import QuizDisagreement
from tests.test_quiz_dom import OfflineAI


class ScreenshotQuizTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='msedge', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page(viewport={'width': 1100, 'height': 760})
        self.page.set_content('<header>ACCOUNT-PRIVATE</header><iframe style="width:1000px;height:600px"></iframe>')
        self.frame = self.page.frames[1]
        self.frame.set_content('<iframe style="width:950px;height:550px"></iframe>')
        self.frame = self.page.frames[2]
        self.adapter = SimpleNamespace(page=self.page, platform='chaoxing',
            guard=lambda **kw: None, visible_frames=lambda: [self.frame], videos=lambda: [], start_attempts=set())
        self.course = CourseActivities(self.adapter, ai_base_url='https://ai.example/v1',
            ai_model='text-model', vision_model='vision-model', ai_api_key='test-key')
        self.course.screenshot_quiz = True
        self.stack = ExitStack()
        for name in ('quiz_dom.ocr_text', 'activities.prompt_text', 'activities._option_text',
                     'quiz_dom.prompt_text', 'quiz_dom.option_text', 'quiz_dom.shared_materials'):
            self.stack.enter_context(patch(name, side_effect=AssertionError('文字提取不得调用: ' + name)))

    def tearDown(self):
        self.stack.close()
        self.page.close()

    def load(self, question=None):
        question = question or '''<fieldset><legend>定位眼睛与鼻梁的技术？</legend>
          <label><input type=radio name=q>物体识别</label>
          <label><input type=radio name=q>人脸关键点检测</label></fieldset>'''
        self.frame.set_content('''<style>label{display:block;margin:30px 0} main{height:340px;overflow:auto}
          fieldset{font:22px Arial;box-sizing:border-box} .font-cxsecret{font-family:font-cxsecret}</style>
          <main><form onsubmit="event.preventDefault();document.querySelector('output').textContent++">
          ''' + question + '<button>提交</button></form></main><output>0</output>')

    def run_ai(self, answers, before=None):
        ai = OfflineAI(answers, before_reply=before)
        with patch('urllib.request.build_opener', return_value=ai):
            self.course.process()
        return ai

    def test_screenshots_reach_vision_without_extracted_text_and_submit_once(self):
        self.load()
        self.course.ai_model = ''  # Screenshot mode needs only the image model.
        ai = self.run_ai([{'id':'0', 'selected':[1], 'confidence':.99}])
        self.assertEqual(self.frame.locator('input:checked').get_attribute('name'), 'q')
        self.assertTrue(self.frame.locator('input').nth(1).is_checked())
        self.assertEqual(self.frame.locator('output').inner_text(), '1')
        self.course.process()
        self.assertEqual(self.frame.locator('output').inner_text(), '1')
        request = ai.requests[0]
        self.assertEqual(request['model'], 'vision-model')
        self.assertNotIn('物体识别', json.dumps(request, ensure_ascii=False))
        self.assertNotIn('ACCOUNT-PRIVATE', json.dumps(request))
        images = [p for p in request['messages'][1]['content'] if p['type']=='image_url']
        self.assertTrue(images)
        png = base64.b64decode(images[0]['image_url']['url'].split(',')[1])
        self.assertTrue(png.startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertGreater(struct.unpack('>II', png[16:24])[0], 500)

    def test_cross_check_votes_before_filling_and_submits_only_once(self):
        from tests.test_quiz_crosscheck import RoundAI
        self.load()
        self.course.quiz_cross_check = True
        self.course.ai_review_model = 'review-vision'
        def answer(index):
            return [{'id': '0', 'selected': [index], 'confidence': .95}]
        observed = []
        ai = RoundAI([answer(0), answer(1), answer(1)],
            after=lambda: observed.append((self.frame.locator('input:checked').count(),
                                           self.frame.locator('output').inner_text())))
        with patch('urllib.request.build_opener', return_value=ai):
            self.course.process()
            self.course.process()
        self.assertEqual(observed, [(0, '0'), (0, '0'), (0, '0')])
        self.assertTrue(self.frame.locator('input').nth(1).is_checked())
        self.assertEqual(self.frame.locator('output').inner_text(), '1')
        self.assertEqual([p['model'] for p in ai.requests],
                         ['vision-model', 'review-vision', 'review-vision'])

    def test_cross_check_conflict_never_fills_or_submits(self):
        from tests.test_quiz_crosscheck import RoundAI
        self.load('<fieldset><legend>Choose</legend>' + ''.join(
            f'<label><input type=radio name=q>Item {i}</label>' for i in range(3)) + '</fieldset>')
        self.course.quiz_cross_check = True
        ai = RoundAI([[{'id': '0', 'selected': [i], 'confidence': .95}] for i in range(3)])
        with patch('urllib.request.build_opener', return_value=ai):
            with self.assertRaisesRegex(ManualRequired, '共识'):
                self.course.process()
        self.assertEqual(len(ai.requests), 3)
        self.assertEqual(self.frame.locator('input:checked').count(), 0)
        self.assertEqual(self.frame.locator('output').inner_text(), '0')

    def test_doubao_conflict_prevents_course_fill_and_submit_without_ocr(self):
        self.load()
        self.course.quiz_cross_check = True
        captured = []
        def review(data, images):
            captured.append((data, images))
            self.assertEqual(self.frame.locator('input:checked').count(), 0)
            return {'answers': [{'id': '0', 'selected': [0], 'confidence': .95}]}
        self.course.web_review = review
        with self.assertRaisesRegex(QuizDisagreement, '本课程已终止'):
            self.run_ai([{'id': '0', 'selected': [1], 'confidence': .95}])
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0][1][0].data.startswith(b'\x89PNG'))
        self.assertNotIn('物体识别', json.dumps(captured[0][0], ensure_ascii=False))
        self.assertEqual(self.frame.locator('input:checked').count(), 0)
        self.assertEqual(self.frame.locator('output').inner_text(), '0')

    def test_doubao_agreement_submits_course_once(self):
        self.load()
        self.course.quiz_cross_check = True
        answers = [{'id': '0', 'selected': [1], 'confidence': .95}]
        self.course.web_review = lambda data, images: {'answers': answers}
        ai = self.run_ai(answers)
        self.course.process()
        self.assertEqual(len(ai.requests), 1)
        self.assertTrue(self.frame.locator('input').nth(1).is_checked())
        self.assertEqual(self.frame.locator('output').inner_text(), '1')

    def test_agreed_answers_survive_interrupted_local_recheck(self):
        self.load(''.join(f'<fieldset><legend>Question {i}</legend>'
            f'<label><input type=radio name=q{i}>A</label>'
            f'<label><input type=radio name=q{i}>B</label></fieldset>' for i in range(5)))
        answers = [dict(id=str(i), selected=[1], confidence=.95) for i in range(5)]
        ai = OfflineAI(answers)
        self.course.quiz_cross_check = True
        review_calls = []
        def review(data, images):
            review_calls.append(1)
            return {'answers': answers}
        self.course.web_review = review
        from quiz_screenshots import capture_region
        def capture(node, check):
            if review_calls:
                raise ManualRequired('截图时页面仍在移动')
            return capture_region(node, check)
        with patch('urllib.request.build_opener', return_value=ai):
            with patch('quiz_screenshots.capture_region', side_effect=capture):
                with self.assertRaisesRegex(ManualRequired, '页面仍在移动'):
                    self.course.process()
            self.assertEqual(self.frame.locator('input:checked').count(), 0)
            self.course.process()
            self.course.process()
        self.assertEqual((len(ai.requests), len(review_calls)), (1, 1))
        self.assertEqual(self.frame.locator('input:checked').count(), 5)
        self.assertEqual(self.frame.locator('output').inner_text(), '1')

    def test_agreed_answers_not_reused_for_replaced_questions(self):
        self.load()
        ai = OfflineAI([dict(id='0', selected=[1], confidence=.95)])
        with patch('urllib.request.build_opener', return_value=ai):
            with patch.object(self.course, '_fill_answers', side_effect=ManualRequired('暂停')):
                with self.assertRaisesRegex(ManualRequired, '暂停'):
                    self.course.process()
            self.frame.locator('legend').evaluate("e=>e.textContent='A different question'")
            ai.answers = [dict(id='0', selected=[0], confidence=.95)]
            self.course.process()
        self.assertEqual(len(ai.requests), 2)
        self.assertTrue(self.frame.locator('input').first.is_checked())
        self.assertEqual(self.frame.locator('output').inner_text(), '1')

    def test_local_recheck_rejects_changed_question_rendered_by_stylesheet(self):
        self.load()
        ai = OfflineAI([dict(id='0', selected=[1], confidence=.95)],
                       before_reply=lambda: self.frame.add_style_tag(
                           content="legend::after {content: ' (revised question)'}"))
        with patch('urllib.request.build_opener', return_value=ai):
            with self.assertRaisesRegex(ManualRequired, '题目内容已变化'):
                self.course.process()
        self.assertEqual(len(ai.requests), 1)
        self.assertEqual(self.frame.locator('input:checked').count(), 0)
        self.assertIsNone(self.course.reviewed_quiz)

    def test_decorative_check_mark_does_not_invalidate_answer(self):
        self.load()
        self.frame.add_style_tag(content='label:has(:checked)::after {content: "✓"}')
        ai = self.run_ai([dict(id='0', selected=[1], confidence=.95)])
        self.assertEqual(len(ai.requests), 1)
        self.assertEqual(self.frame.locator('output').inner_text(), '1')

    def test_transient_movement_during_tile_capture_recovers(self):
        from quiz_capture import _visible_rect, capture_region
        self.load()
        readings = []
        def moved_once(marker):
            rect = _visible_rect(marker)
            readings.append(1)
            if len(readings) == 2:
                return dict(rect, top=rect['top'] + 2)
            return rect
        with patch('quiz_capture._visible_rect', side_effect=moved_once):
            png = capture_region(self.frame.locator('fieldset'))
        self.assertGreaterEqual(len(readings), 4)
        self.assertTrue(png.startswith(b'\x89PNG'))

    def test_continuous_movement_during_tile_capture_stops(self):
        from quiz_capture import _visible_rect, capture_region
        self.load()
        readings = []
        def moving(marker):
            rect = _visible_rect(marker)
            readings.append(1)
            return dict(rect, top=rect['top'] + (2 if len(readings) % 2 == 0 else 0))
        with patch('quiz_capture._visible_rect', side_effect=moving):
            with self.assertRaisesRegex(ManualRequired, '仍在移动'):
                capture_region(self.frame.locator('fieldset'))
        self.assertEqual(len(readings), 6)

    def test_tall_question_captures_bottom_choice_and_scrolls_to_submit(self):
        self.load('''<fieldset style="height:1150px"><legend>Find the blue bottom option</legend>
          <label><input type=radio name=q>Top</label><div style="height:700px"></div>
          <label style="background:rgb(0,100,255)"><input type=radio name=q>Bottom</label></fieldset>''')
        ai = self.run_ai([{'id':'0','selected':[1],'confidence':.99}])
        images = [p for p in ai.requests[0]['messages'][1]['content'] if p['type']=='image_url']
        png = base64.b64decode(images[0]['image_url']['url'].split(',')[1])
        self.assertGreaterEqual(struct.unpack('>II', png[16:24])[1], 1148)
        # Inspect actual image pixels in a detached canvas; no OCR involved.
        blue = self.page.evaluate('''async data=>{const i=new Image();i.src=data;await i.decode();
          const c=document.createElement('canvas');c.width=i.width;c.height=i.height;
          const g=c.getContext('2d');g.drawImage(i,0,0);const d=g.getImageData(0,750,c.width,c.height-750).data;
          let count=0;for(let n=0;n<d.length;n+=4)if(d[n]<10&&d[n+1]===100&&d[n+2]===255)count++;
          return count}''', images[0]['image_url']['url'])
        self.assertGreater(blue, 1000)
        self.assertEqual(self.frame.locator('output').inner_text(), '1')
        self.assertEqual(self.frame.locator('[data-ch-capture]').count(), 0)

    def test_scroll_tiles_retain_each_band_at_reduced_page_zoom(self):
        from quiz_capture import capture_region
        self.load('<div class="TiMu" style="zoom:.75;width:720px">' + ''.join(
            f'<div style="height:160px;background:rgb({20*i},80,120)"></div>'
            for i in range(10)) + '</div>')
        png = capture_region(self.frame.locator('.TiMu'))
        colors = self.page.evaluate('''async data=>{
          const i=new Image();i.src='data:image/png;base64,'+data;await i.decode();
          const c=document.createElement('canvas');c.width=i.width;c.height=i.height;
          const g=c.getContext('2d');g.drawImage(i,0,0);
          return Array.from({length:10},(_,k)=>[2,60,117].map(d=>
            g.getImageData(40,120*k+d,1,1).data[0]));
        }''', base64.b64encode(png).decode())
        self.assertEqual(colors, [[i*20]*3 for i in range(10)])

    def test_nested_option_fieldset_keeps_stem_in_its_own_question_image(self):
        self.load('<div class=TiMu><div style="height:45px;background:magenta">Stem</div>'
                  '<fieldset><label><input type=radio name=q>First</label>'
                  '<label><input type=radio name=q>Last</label></fieldset></div>')
        ai = self.run_ai([{'id':'0','selected':[1],'confidence':.99}])
        parts = ai.requests[0]['messages'][1]['content']
        index = next(i for i,p in enumerate(parts) if p.get('text')=='图片编号：question_0')
        url = parts[index+1]['image_url']['url']
        self.assertTrue(self.page.evaluate('''async url=>{
          const i=new Image();i.src=url;await i.decode();const c=document.createElement('canvas');
          c.width=i.width;c.height=i.height;const g=c.getContext('2d');g.drawImage(i,0,0);
          const [r,b,v]=g.getImageData(i.width-50,10,1,1).data;return r===255&&b===0&&v===255;
        }''',url))

    def test_six_questions_fill_and_submit_only_after_last_batch(self):
        from tests.test_quiz_crosscheck import RoundAI
        questions = ''.join(f'<div class=TiMu><p>Question {i}</p><fieldset>'
            f'<label><input name=q{i} type=radio>A</label>'
            f'<label><input name=q{i} type=radio>B</label></fieldset></div>' for i in range(6))
        expected = [dict(id=str(i), selected=[1], confidence=.98) for i in range(6)]
        def unchanged():
            self.assertEqual(self.frame.locator('input:checked').count(), 0)
            self.assertEqual(self.frame.locator('output').inner_text(), '0')
        for valid in (False, True):
            self.load(questions)
            ai = RoundAI([expected[:5], expected[5:] if valid else []], after=unchanged)
            with patch('urllib.request.build_opener', return_value=ai):
                if valid:
                    self.course.process()
                    self.assertEqual(self.frame.locator('input:checked').count(), 6)
                    self.assertEqual(self.frame.locator('output').inner_text(), '1')
                else:
                    with self.assertRaisesRegex(ManualRequired, '第 6'):
                        self.course.process()
                    unchanged()
            self.assertEqual(len(ai.requests), 2)

    def test_fill_answer_survives_content_recheck(self):
        self.load('<fieldset><legend>Complete</legend>We <input name=blank> here.</fieldset>')
        self.run_ai([{'id':'0','filled':{'q0_b0':'are'},'confidence':.99}])
        self.assertEqual(self.frame.locator('input').input_value(), 'are')
        self.assertEqual(self.frame.locator('output').inner_text(), '1')

    def test_content_change_while_ai_thinks_blocks_all_edits(self):
        self.load()
        with self.assertRaisesRegex(ManualRequired, '变化'):
            self.run_ai([{'id':'0','selected':[1],'confidence':.99}],
                lambda: self.frame.locator('legend').evaluate("e=>e.textContent='Another question'"))
        self.assertEqual(self.frame.locator('input:checked').count(), 0)
        self.assertEqual(self.frame.locator('output').inner_text(), '0')

    def test_missing_vision_model_never_falls_back_to_ocr(self):
        self.load()
        self.course.vision_model = ''
        with self.assertRaisesRegex(ManualRequired, '图片模型|视觉模型'):
            self.course.process()

    def test_three_special_font_questions_scroll_inside_nested_frames(self):
        self.page.locator('header').evaluate("e=>e.style.cssText='position:fixed;top:0;height:45px;background:white;z-index:99'")
        self.page.locator('iframe').evaluate("e=>e.style.marginTop='55px'")
        questions = ''.join(f'''<div class="TiMu font-cxsecret" style="font:22px Arial;padding:20px 0">
          <p>Question {i}</p><ul>
          <li role=radio aria-checked=false onclick="for(const n of this.parentElement.children)n.setAttribute('aria-checked',n===this)">A. One</li>
          <li role=radio aria-checked=false onclick="for(const n of this.parentElement.children)n.setAttribute('aria-checked',n===this)">B. Two</li></ul></div>'''
          for i in range(3))
        self.load(questions)
        self.run_ai([{'id':str(i),'selected':[1],'confidence':.99} for i in range(3)])
        self.assertEqual(self.frame.locator('[aria-checked=true]').count(), 3)
        self.assertEqual(self.frame.locator('output').inner_text(), '1')

    def test_shared_passage_is_an_image_not_extracted_text(self):
        self.load('<article data-passage style="font:22px Arial">UNIQUE_SHARED_PASSAGE</article>'
                  '<fieldset><legend>Choose based on passage</legend>'
                  '<label><input type=radio name=q>one</label><label><input type=radio name=q>two</label></fieldset>')
        ai = self.run_ai([{'id':'0','selected':[1],'confidence':.99}])
        request = ai.requests[0]
        self.assertNotIn('UNIQUE_SHARED_PASSAGE', json.dumps(request))
        self.assertEqual(sum(p['type']=='image_url' for p in request['messages'][1]['content']), 2)

    def test_plain_material_and_inner_scroller_are_complete_images(self):
        self.load('<div style="height:44px;background:rgb(255,0,255)">Shared image material</div>'
            '<fieldset><legend>Use all material</legend>'
            '<div class=reading-passage style="height:60px;overflow:auto">'
            '<div style="height:500px"></div><div style="height:40px;background:rgb(255,0,255)">Bottom fact</div></div>'
            '<label><input type=radio name=q>A</label><label><input type=radio name=q>B</label></fieldset>')
        original = self.frame.locator('.reading-passage').get_attribute('style')
        ai = self.run_ai([{'id':'0','selected':[0],'confidence':.99}])
        images = [p for p in ai.requests[0]['messages'][1]['content'] if p['type']=='image_url']
        self.assertEqual(len(images), 2)
        for pic in images:
            magenta = self.page.evaluate('''async data=>{const i=new Image();i.src=data;await i.decode();
              const c=document.createElement('canvas');c.width=i.width;c.height=i.height;const g=c.getContext('2d');
              g.drawImage(i,0,0);const d=g.getImageData(0,0,c.width,c.height).data;
              let n=0;for(let j=0;j<d.length;j+=4)if(d[j]===255&&d[j+1]===0&&d[j+2]===255)n++;return n}''', pic['image_url']['url'])
            self.assertGreater(magenta, 1000)
        self.assertEqual(self.frame.locator('.reading-passage').get_attribute('style'), original)

    def test_hidden_material_is_not_silently_omitted(self):
        self.load('<div class=reading-passage hidden>Required paragraph</div>'
            '<fieldset><legend>Answer</legend><label><input type=radio name=q>A</label>'
            '<label><input type=radio name=q>B</label></fieldset>')
        with self.assertRaisesRegex(ManualRequired, '材料.*隐藏|材料.*展开'):
            self.course.process()
        self.assertEqual(self.frame.locator('output').inner_text(), '0')

    def test_flex_column_material_expands_without_cutting_bottom_pixels(self):
        self.load('<fieldset><legend>Use the whole panel</legend>'
            '<div style="display:flex;flex-direction:column;height:80px">'
            '<div class=reading-passage style="overflow:auto;min-height:0">'
            '<div style="height:400px">Start</div>'
            '<div style="height:40px;background:rgb(255,0,255)">Bottom</div></div></div>'
            '<label><input type=radio name=q>A</label><label><input type=radio name=q>B</label></fieldset>')
        self._assert_full_magenta_screenshot_and_restored_layout()

    def test_flex_row_material_expands_without_cutting_right_pixels(self):
        self.load('<fieldset><legend>Use the whole row</legend>'
            '<div style="display:flex;width:250px">'
            '<div class=reading-passage style="overflow:hidden;white-space:nowrap">'
            '<span style="display:inline-block;width:450px">Start</span>'
            '<span style="display:inline-block;width:80px;height:40px;background:rgb(255,0,255)">End</span>'
            '</div><span>Fixed sibling</span></div>'
            '<label><input type=radio name=q>A</label><label><input type=radio name=q>B</label></fieldset>')
        self._assert_full_magenta_screenshot_and_restored_layout()

    def test_multichoice_list_that_grows_with_clip_keeps_all_ten_rightmost_pixels(self):
        self._check_expanding_multichoice(360, 'border-right:10px solid magenta')

    def test_multichoice_full_width_padding_preserved_across_horizontal_tiles(self):
        self._check_expanding_multichoice(1167,
            'padding-right:10px;background:linear-gradient(to left,magenta 10px,white 10px)')

    def _check_expanding_multichoice(self, width, edge):
        self.load(f'''<fieldset style="width:{width}px;padding:0;border:0"><legend>Select both</legend>
          <div class="clearfix" style="width:100%;overflow:hidden">
            <ul style="width:100%;box-sizing:content-box;list-style:none;margin:0;padding:0;{edge}">
              <li style="height:90px"><label style="margin:0"><input type=checkbox>First</label></li>
              <li style="height:90px"><label style="margin:0"><input type=checkbox>Second</label></li>
            </ul><input type=hidden><input type=hidden>
          </div></fieldset>''')
        overflow = self.frame.locator('.clearfix').evaluate('e=>e.scrollWidth-e.clientWidth')
        self.assertEqual(overflow, 10)
        # Keep every pixel beyond the original root, even across horizontal screenshot tiles.
        self._assert_full_magenta_screenshot_and_restored_layout(selected=[0,1], exact_pixels=1800)
        self.assertEqual(self.frame.locator('input:checked').count(), 2)

    def _assert_full_magenta_screenshot_and_restored_layout(self, selected=None, exact_pixels=None):
        # Screenshot caret handling may leave an empty inline style on a hidden input.
        # Compare rendered layout and the original inline CSS of every visible element.
        def layout():
            return self.frame.locator('fieldset').evaluate('''e=>[e,...e.querySelectorAll('*')]
                .filter(n=>n.getClientRects().length).map(n=>[n.tagName,n.getAttribute('style'),
                    n.offsetWidth,n.offsetHeight,n.clientWidth,n.clientHeight,n.scrollWidth,n.scrollHeight])''')
        before = layout()
        ai = self.run_ai([{'id':'0','selected':selected or [0],'confidence':.99}])
        pics = [p for p in ai.requests[0]['messages'][1]['content'] if p['type']=='image_url']
        counts = [self.page.evaluate('''async data=>{
          const i=new Image();i.src=data;await i.decode();const c=document.createElement('canvas');
          c.width=i.width;c.height=i.height;const g=c.getContext('2d');g.drawImage(i,0,0);
          const d=g.getImageData(0,0,c.width,c.height).data;let n=0;
          for(let j=0;j<d.length;j+=4)if(d[j]===255&&d[j+1]===0&&d[j+2]===255)n++;
          return n}''', pic['image_url']['url']) for pic in pics]
        if exact_pixels is None:
            self.assertGreater(max(counts), 2000)
        else:
            self.assertEqual(max(counts), exact_pixels)
        self.assertEqual(layout(), before)
        self.assertEqual(self.frame.locator('output').inner_text(), '1')

    def test_long_select_options_expand_horizontally_and_restore_width(self):
        self.load('<fieldset><legend>Choose</legend><select name=q style="width:45px">'
            '<option value="">Choose</option>'
            '<option value=long>A long answer option whose final words must be visible</option>'
            '<option value=short>Short</option></select></fieldset>')
        from quiz_layout import expanded_region
        select = self.frame.locator('select')
        before = select.get_attribute('style')
        with expanded_region(self.frame.locator('fieldset')):
            size = select.evaluate('e=>({visible:e.clientWidth,content:e.scrollWidth})')
            self.assertGreater(size['visible'], 250)
            self.assertLessEqual(size['content'], size['visible'] + 2)
        self.assertEqual(select.get_attribute('style'), before)
        self.assertIsNone(select.get_attribute('size'))
        self.run_ai([{'id':'0','filled':{'q0_b0':'opt_0'},'confidence':.99}])
        self.assertEqual(select.input_value(), 'long')
        self.assertEqual(select.get_attribute('style'), before)
        self.assertEqual(self.frame.locator('output').inner_text(), '1')

    def test_clipped_oversize_material_still_stops_and_restores_layout(self):
        self.load('<fieldset><legend>Choose</legend>'
            '<div class=reading-passage style="height:60px;overflow:hidden">'
            '<div style="height:9000px">Too long</div></div>'
            '<label><input type=radio name=q>A</label><label><input type=radio name=q>B</label></fieldset>')
        before = self.frame.locator('fieldset').evaluate('e=>e.outerHTML')
        with patch('urllib.request.build_opener', side_effect=AssertionError('Incomplete material sent')):
            with self.assertRaisesRegex(ManualRequired, r'第 1 题仍有纵向裁剪：可见 60 像素，内容 9000 像素'):
                self.course.process()
        self.assertEqual(self.frame.locator('fieldset').evaluate('e=>e.outerHTML'), before)
        self.assertEqual(self.frame.locator('output').inner_text(), '0')

    def test_expansion_restores_scrolled_inner_panel_after_failure(self):
        self.load('<fieldset><legend>Choose</legend><div class=reading-passage '
            'style="height:60px;overflow:auto"><div style="height:500px">Long</div></div>'
            '<label><input type=radio name=q>A</label><label><input type=radio name=q>B</label></fieldset>')
        from quiz_layout import expanded_region
        panel = self.frame.locator('.reading-passage')
        panel.evaluate('e=>e.scrollTop=120')
        before = panel.get_attribute('style')
        with self.assertRaisesRegex(ManualRequired, 'cancel'):
            with expanded_region(self.frame.locator('fieldset')):
                raise ManualRequired('cancel')
        self.assertEqual(panel.get_attribute('style'), before)
        self.assertEqual(panel.evaluate('e=>e.scrollTop'), 120)

    def test_account_and_header_wrappers_are_not_shared_material(self):
        self.load('<div class=account-card>PRIVATE_ACCOUNT_CARD</div>'
            '<div><header>PRIVATE_ACCOUNT_HEADER</header><p data-passage>Required passage</p></div>'
            '<fieldset><legend>Choose</legend><label><input type=radio name=q>A</label>'
            '<label><input type=radio name=q>B</label></fieldset>')
        from quiz_screenshots import material_regions
        materials = material_regions(self.course._detect())
        self.assertEqual(len(materials), 1)
        self.assertTrue(materials[0].evaluate('e=>e.matches("[data-passage]")'))

    def test_without_quiz_form_only_explicit_material_is_captured(self):
        self.load('<div>PRIVATE_UNMARKED_CARD</div><p data-passage>Required passage</p>'
            '<fieldset><legend>Choose</legend><label><input type=radio name=q>A</label>'
            '<label><input type=radio name=q>B</label></fieldset>')
        self.frame.locator('form').evaluate('e=>e.replaceWith(...e.childNodes)')
        from quiz_screenshots import material_regions
        materials = material_regions(self.course._detect())
        self.assertEqual(len(materials), 1)
        self.assertTrue(materials[0].evaluate('e=>e.matches("[data-passage]")'))

    def test_material_text_mixed_with_account_header_stops_before_ai(self):
        for attributes in ('class=reading-passage', ''):
            with self.subTest(attributes=attributes):
                self.load('<div ' + attributes + '>Required first paragraph.'
                    '<header>Student: PRIVATE</header></div>'
                    '<fieldset><legend>Choose</legend><label><input type=radio name=q>A</label>'
                    '<label><input type=radio name=q>B</label></fieldset>')
                with patch('urllib.request.build_opener', side_effect=AssertionError('Partial material sent')):
                    with self.assertRaisesRegex(ManualRequired, '材料|混有'):
                        self.course.process()
                self.assertEqual(self.frame.locator('input:checked').count(), 0)
                self.assertEqual(self.frame.locator('output').inner_text(), '0')

    def test_collapsed_or_hidden_material_descendants_block_before_ai(self):
        for fragment in ('<details><summary>Expand</summary><p>Required fact</p></details>',
                         '<p style="display:none">Required fact</p>'):
            with self.subTest(fragment=fragment):
                self.load('<div class=reading-passage>Visible introduction' + fragment + '</div>'
                    '<fieldset><legend>Choose</legend><label><input type=radio name=q>A</label>'
                    '<label><input type=radio name=q>B</label></fieldset>')
                with patch('urllib.request.build_opener', side_effect=AssertionError('Incomplete material sent')):
                    with self.assertRaisesRegex(ManualRequired, '隐藏|折叠|展开'):
                        self.course.process()
                self.assertEqual(self.frame.locator('input:checked').count(), 0)
                self.assertEqual(self.frame.locator('output').inner_text(), '0')

    def test_chaoxing_multiple_choice_check_answer_dx_is_answer_state(self):
        self.load('<div class=TiMu><p>Choose both</p><ul>'
            '<li role=checkbox onclick="this.firstElementChild.classList.toggle(\'check_answer_dx\')"><span class=num_option_dx>A</span> First</li>'
            '<li role=checkbox onclick="this.firstElementChild.classList.toggle(\'check_answer_dx\')"><span class=num_option_dx>B</span> Second</li></ul></div>')
        self.run_ai([{'id':'0','selected_labels':['B','A'],'confidence':.99}])
        self.assertEqual(self.frame.locator('.check_answer_dx').all_text_contents(), ['A', 'B'])
        self.assertEqual(self.frame.locator('output').inner_text(), '1')

    def test_ordered_by_page_letter_in_mixed_quiz(self):
        self.load('''<fieldset><legend>Generic</legend><label><input type=radio name=q>X</label>
            <label><input type=radio name=q>Y</label></fieldset>
            <div class=TiMu><p>选出符合题意的一项</p><ul>''' +
            ''.join(f'''<li role=radio aria-checked=false onclick="this.setAttribute('aria-checked','true')">
            <span class=num_option>{letter}</span>{word}</li>''' for letter, word in
            [('C', '第三种解释'), ('A', '第一种解释'), ('B', '第二种解释')]) + '</ul></div>')
        with self.assertLogs(level='INFO') as logs:
            ai = self.run_ai([{'id':'1','selected_labels':['B'],'confidence':.99},
                              {'id':'0','selected':[1],'confidence':.99}])
        self.assertEqual(len(ai.requests), 1)
        content = ai.requests[0]['messages'][1]['content']
        questions = json.loads(content[0]['text'])['questions']
        self.assertEqual(questions[1]['options'], ['C','A','B'])
        self.assertNotIn('第二种解释', json.dumps(content, ensure_ascii=False))
        self.assertEqual(self.frame.locator('[aria-checked=true] .num_option').inner_text(), 'B')
        self.assertTrue(self.frame.locator('input').nth(1).is_checked())
        self.assertTrue(any('第 2 题' in line and 'AI 选择 B，网页已选 B' in line for line in logs.output))
        self.assertEqual(self.frame.locator('output').inner_text(), '1')

    def test_page_letter_requires_unambiguous_model_and_control_labels(self):
        for letters, field, value in [
                ('AB', 'selected', [1]), ('AB', 'selected_labels', ['C']),
                ('AB', 'selected_labels', ['A', 'A']), ('AA', 'selected_labels', ['A'])]:
            with self.subTest(letters=letters, field=field, value=value):
                self.load('<div class=TiMu><p>Choose</p><ul>' +
                    ''.join(f'<li role=radio aria-checked=false><span class=num_option>{c}</span>选项</li>'
                            for c in letters) + '</ul></div>')
                with self.assertRaises(ManualRequired):
                    self.run_ai([{'id':'0',field:value,'confidence':.99}])
                self.assertEqual(self.frame.locator('output').inner_text(), '0')

    def test_select_choices_are_screenshotted_and_map_to_values(self):
        self.load('<fieldset><legend>Pick two</legend><select name=q>'
            '<option value="">Choose</option><option value=one>ONE</option>'
            '<option value=two>TWO</option></select></fieldset>')
        ai = self.run_ai([{'id':'0','filled':{'q0_b0':'opt_1'},'confidence':.99}])
        self.assertEqual(self.frame.locator('select').input_value(), 'two')
        self.assertIsNone(self.frame.locator('select').get_attribute('size'))
        self.assertNotIn('TWO', json.dumps(ai.requests[0]))
        self.assertEqual(self.frame.locator('output').inner_text(), '1')

    def test_low_confidence_and_hidden_questions_do_not_submit(self):
        self.load()
        with self.assertRaises(ManualRequired):
            self.run_ai([{'id':'0','selected':[1],'confidence':.4}])
        self.assertEqual(self.frame.locator('input:checked').count(), 0)
        self.frame.locator('form').evaluate("e=>e.insertAdjacentHTML('beforeend','<fieldset hidden><input></fieldset>')")
        with self.assertRaisesRegex(ManualRequired, '隐藏|折叠'):
            self.course.process()
        self.assertEqual(self.frame.locator('output').inner_text(), '0')

    def test_canvas_change_before_reply_is_detected_without_edit(self):
        self.load('<fieldset><legend>What color?</legend><canvas width=60 height=60></canvas>'
            '<label><input type=radio name=q>Red</label><label><input type=radio name=q>Blue</label></fieldset>')
        draw = lambda color: self.frame.locator('canvas').evaluate(
            "(e,c)=>{const g=e.getContext('2d');g.fillStyle=c;g.fillRect(0,0,60,60)}", color)
        draw('red')
        with self.assertRaisesRegex(ManualRequired, '变化'):
            self.run_ai([{'id':'0','selected':[0],'confidence':.99}], lambda: draw('blue'))
        self.assertEqual(self.frame.locator('input:checked').count(), 0)

    def test_canvas_change_during_fill_prevents_submit(self):
        self.load('<fieldset><legend>What color?</legend><canvas width=60 height=60></canvas>'
            '<label><input type=radio name=q onclick="const g=document.querySelector(\'canvas\').getContext(\'2d\');g.fillStyle=\'blue\';g.fillRect(0,0,60,60)">Red</label>'
            '<label><input type=radio name=q>Blue</label></fieldset>')
        self.frame.locator('canvas').evaluate("e=>{const g=e.getContext('2d');g.fillStyle='red';g.fillRect(0,0,60,60)}")
        with self.assertRaisesRegex(ManualRequired, '变化'):
            self.run_ai([{'id':'0','selected':[0],'confidence':.99}])
        self.assertEqual(self.frame.locator('output').inner_text(), '0')

    def test_submission_style_change_does_not_create_another_attempt(self):
        self.load()
        self.frame.locator('form').evaluate('''e=>e.onsubmit=event=>{
          event.preventDefault();document.querySelector('output').textContent++;
          document.querySelector('fieldset').classList.add('submitted');
          e.insertAdjacentHTML('beforeend','<p role=status>Submitted successfully</p>');
        }''')
        ai = OfflineAI([{'id':'0','selected':[1],'confidence':.99}])
        with patch('urllib.request.build_opener', return_value=ai):
            for _ in range(4):
                self.course.process()
        self.assertEqual(len(ai.requests), 1)
        self.assertEqual(self.frame.locator('output').inner_text(), '1')

    def test_cancellation_cleans_up_temporary_marker_and_style(self):
        from quiz_capture import capture_region
        self.load()
        calls = []
        def cancel():
            calls.append(True)
            if len(calls)>2:
                raise ManualRequired('cancel-test')
        group = self.frame.locator('fieldset')
        with self.assertRaisesRegex(ManualRequired, 'cancel-test'):
            capture_region(group, cancel)
        self.assertEqual(self.frame.locator('[data-ch-capture]').count(), 0)
        self.assertIsNone(group.get_attribute('style'))

    def test_shared_audio_is_attached_without_ocr(self):
        self.load('<div data-passage>Listen to the recording<audio src="data:audio/wav;base64,AAAA"></audio></div>'
            '<fieldset><legend>Listen</legend><label><input type=radio name=q>A</label>'
            '<label><input type=radio name=q>B</label></fieldset>')
        with patch('quiz_screenshots.audio_bytes', return_value=(b'synthetic-wave', 'wav')):
            questions, _ = self.course._read_quiz(self.course._detect())
        self.assertEqual(len(questions[0].audio), 1)
        self.assertEqual(questions[0].audio[0].data, b'synthetic-wave')

    def test_absolute_external_layout_stops_without_changing_position(self):
        from quiz_capture import capture_region
        self.load('<fieldset><legend>Layout</legend><div style="position:absolute;top:15px">Material</div>'
            '<label><input type=radio name=q>A</label><label><input type=radio name=q>B</label></fieldset>')
        before = self.frame.locator('fieldset div').bounding_box()
        with self.assertRaisesRegex(ManualRequired, '绝对定位'):
            capture_region(self.frame.locator('fieldset'))
        self.assertEqual(before, self.frame.locator('fieldset div').bounding_box())

    def test_standalone_material_image_keeps_pixels(self):
        self.load('<img width=80 height=80 src="data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' width=\'80\' height=\'80\'%3E%3Crect width=\'80\' height=\'80\' fill=\'red\'/%3E%3C/svg%3E">'
            '<fieldset><legend>Color?</legend><label><input type=radio name=q>Red</label>'
            '<label><input type=radio name=q>Blue</label></fieldset>')
        questions, _ = self.course._read_quiz(self.course._detect())
        self.assertEqual(len(questions[0].images), 2)
        self.assertEqual(struct.unpack('>II', questions[0].images[1].data[16:24]), (80, 80))

