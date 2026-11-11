"""超星截图答题填完一整组后，校验题目身份和真实选中状态。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

from activities import CourseActivities
from browser import ManualRequired
from tests.test_quiz_dom import OfflineAI


class PostfillIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel='msedge', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.set_content('<iframe style="width:95vw;height:88vh"></iframe>')
        self.frame = page.frames[1]
        adapter = SimpleNamespace(page=page, platform='chaoxing', visible_frames=lambda: [self.frame],
                                  guard=lambda **kw: None, videos=lambda: [])
        self.course = CourseActivities(adapter, ai_base_url='https://offline.invalid/v1',
                                      vision_model='offline-vision', ai_api_key='test')
        self.course.screenshot_quiz = True
        self.course.quiz_cross_check = True

    def load(self, choices, expected):
        groups = []
        for index, (role, letters) in enumerate(choices):
            circle = 'num_option' if role == 'radio' else 'num_option_dx'
            options = ''.join(
                f'<li role="{role}" aria-label="{label} 选项{label}选择">'
                f'<label><span class="{circle}">{label}</span></label>'
                f'<a class="after">选项{label}</a></li>' for label in letters)
            groups.append(f'<div class="TiMu newTiMu"><div class="Zy_TItle">'
                          f'<i>{index+1}</i><div class="fontLabel">练习 {index+1}</div></div>'
                          f'<ul>{options}</ul></div>')
        self.frame.set_content('<style>li{display:block;padding:4px}</style><main><form>'
                               + ''.join(groups) + '<button>提交</button></form><output>0</output></main>')
        self.frame.evaluate("""() => {
          document.querySelector('form').onsubmit = event => {
            event.preventDefault();
            document.querySelector('output').textContent++;
          };
          for (const li of document.querySelectorAll('li')) {
            li.onclick = () => {
              const radio = li.getAttribute('role') === 'radio';
              for (const option of radio ? li.parentNode.children : [li]) {
                const marker = option.querySelector('span');
                const state = radio ? 'check_answer' : 'check_answer_dx';
                const checked = radio ? option === li : !marker.classList.contains(state);
                marker.classList.toggle(state, checked);
                option.setAttribute('aria-label', marker.textContent + ' ' +
                    option.querySelector('.after').textContent + (checked ? '已选择' : '选择'));
              }
            };
          }
        }""")
        answers = [dict(id=str(i), selected_labels=list(labels), confidence=.65 if i == 1 else .98)
                   for i, labels in enumerate(expected)]
        self.primary = OfflineAI(answers)
        review_answers = [dict(a, confidence=.74 if a['id'] == '1' else .98) for a in answers]
        # 只允许调用本身；否则 Mock 会让 with_updates 也变成可调用对象，结果不是答案对象。
        self.review = Mock(spec=['__call__'], return_value={'answers': review_answers})
        self.course.web_review = self.review
        self.enterContext(patch('urllib.request.build_opener', return_value=self.primary))

    def test_five_agreed_answers_survive_chaoxing_selection_labels_and_submit_once(self):
        expected = ['A', 'B', 'ABC', 'ABC', 'A']
        self.load([('radio', 'ABCD'), ('radio', 'ABCD'), ('checkbox', 'ABCD'),
                   ('checkbox', 'ABCD'), ('radio', 'AB')], expected)
        self.course.process()
        self.course.process()
        self.assertEqual(self.frame.locator('.TiMu').evaluate_all("""groups => groups.map(group =>
            [...group.querySelectorAll('.check_answer,.check_answer_dx')].map(n=>n.textContent).join(''))
            """), expected)
        self.assertEqual(self.frame.locator('output').inner_text(), '1')
        self.assertEqual((len(self.primary.requests), self.review.call_count), (1, 1))

    def test_changed_option_wording_during_fill_blocks_submission(self):
        self.load([('radio', 'AB')], ['A'])
        self.frame.locator('li').first.evaluate("""li => li.addEventListener('click', () => {
          li.parentNode.lastElementChild.querySelector('.after').textContent = '更新后的选项';
        })""")
        with self.assertRaisesRegex(ManualRequired, '变化'):
            self.course.process()
        self.assertEqual(self.frame.locator('output').inner_text(), '0')
        self.assertEqual((len(self.primary.requests), self.review.call_count), (1, 1))
