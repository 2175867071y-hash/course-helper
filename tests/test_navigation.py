"""在自建 HTML 中验证通用目录、延迟切页、重复标题及定位操作。"""
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from browser import Adapter, ManualRequired
from core import validate_url
from gui_backend import GuiRequest, resolve_target


class GenericEntryTests(unittest.TestCase):
    def test_accepts_arbitrary_https_course_and_retains_existing_platform_checks(self):
        url = 'https://courses.example.test/learn/one'
        self.assertEqual(validate_url(url, 'generic'), url)
        self.assertEqual(resolve_target(GuiRequest('run', 'generic', 'generic:test', url), None), url)
        with self.assertRaises(ValueError):
            validate_url(url, 'uai')

    def test_generic_requires_url_and_rejects_non_web_schemes_and_credentials(self):
        with self.assertRaisesRegex(ValueError, '课程链接'):
            resolve_target(GuiRequest('run', 'generic', 'generic:test', resume=False), None)
        for url in ('file:///C:/test', 'javascript:alert(1)', 'http://course.test/',
                    'https://a:b@course.test/', 'https://course.test\\@evil.test/'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url, 'generic')


class NavigationDOMTests(unittest.TestCase):
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
        self.page.route('https://courses.example.test/**',
                        lambda route: route.fulfill(body='<html><body></body></html>'))
        self.page.goto('https://courses.example.test/learn/one')
        self.adapter = Adapter(self.page, 'generic')

    def tearDown(self):
        self.page.close()

    def load(self, extra='', delay=0, change=True):
        self.page.set_content('''<nav aria-label="课程目录"><h3>Section A</h3>
            <button aria-current="step">Reading</button><h3>Section B</h3>
            <button>Reading</button><button>Quiz</button></nav>
            <main><article>Original content to read.</article></main>''' + extra + '''<script>
            window.clicks=0;
            for (const [i,b] of [...document.querySelectorAll('nav button')].entries()) {
              b.onclick=()=>{window.clicks++;
                document.querySelectorAll('nav button').forEach(e=>e.removeAttribute('aria-current'));
                b.setAttribute('aria-current','step');
                if (CHANGE) setTimeout(()=>document.querySelector('article').textContent='New content '+i, DELAY);
              };
            }</script>'''.replace('CHANGE', str(change).lower()).replace('DELAY', str(delay)))

    def test_next_directory_item_is_selected_by_position_not_duplicate_title(self):
        self.load()
        target = self.adapter.next_control()
        self.assertIsNotNone(target, '需要支持目录，而不只是下一节按钮')
        target.click(timeout=1500)
        self.assertEqual(self.page.locator('nav button').nth(1).get_attribute('aria-current'), 'step')
        self.assertEqual(self.page.evaluate('window.clicks'), 1)

    def test_explicit_english_next_lesson_is_supported(self):
        self.page.set_content('<main><button>Next lesson</button></main>')
        self.assertIsNotNone(self.adapter.next_control())

    def test_relative_next_lesson_link_is_valid(self):
        self.page.set_content('<main><a href="/learn/two">Next lesson</a></main>')
        self.assertIsNotNone(self.adapter.next_control())

    def test_full_page_directory_link_confirms_new_content(self):
        from activities import CourseActivities
        self.page.route('https://courses.example.test/learn/two', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body='''
            <nav aria-label="课程目录"><a href="/learn/one">One</a>
            <a href="/learn/two" aria-current="page">Two</a></nav>
            <main><article>Second lesson content.</article></main>'''))
        self.page.set_content('''<nav aria-label="课程目录"><a href="/learn/one" aria-current="page">One</a>
            <a href="/learn/two">Two</a></nav><main><article>First lesson content.</article></main>''')
        course = CourseActivities(self.adapter)
        self.adapter.advance_activity(course.identity(), timeout=1.5)
        self.assertIsNone(self.adapter.navigator.pending)
        self.assertTrue(self.page.url.endswith('/learn/two'))

    def test_generic_does_not_follow_external_directory_link(self):
        self.page.set_content('<nav aria-label="课程目录"><a aria-current="page">A</a>'
                              '<a href="https://other.test/lesson">B</a></nav>')
        with self.assertRaisesRegex(ManualRequired, '地址|站点'):
            self.adapter.next_control()

    def test_disabled_immediate_next_is_not_skipped(self):
        self.load()
        self.page.locator('nav button').nth(1).evaluate("e=>e.disabled=true")
        with self.assertRaisesRegex(ManualRequired, '锁定|不可用'):
            self.adapter.next_control()

    def test_two_catalogues_with_active_item_are_ambiguous(self):
        self.load('<nav aria-label="章节目录"><button aria-current="step">Other</button><button>Next</button></nav>')
        with self.assertRaisesRegex(ManualRequired, '多个|唯一'):
            self.adapter.next_control()

    def test_delayed_content_must_arrive_before_navigation_completes(self):
        from activities import CourseActivities
        self.load(delay=700)
        course = CourseActivities(self.adapter)
        self.assertTrue(course.identity(), '纯文字 main/article 页面应可识别')
        old = course.identity()
        self.adapter.advance_activity(old, timeout=3)
        self.assertEqual(self.page.locator('article').inner_text(), 'New content 1')
        self.assertNotEqual(course.identity(), old)
        self.assertEqual(self.page.evaluate('window.clicks'), 1)

    def test_highlight_only_does_not_count_as_success_and_is_not_clicked_twice(self):
        from activities import CourseActivities
        self.load(change=False)
        course = CourseActivities(self.adapter)
        old = course.identity()
        self.assertTrue(old)
        for _ in range(2):
            with self.assertRaises(ManualRequired):
                self.adapter.advance_activity(old, timeout=0.3)
        self.assertEqual(self.page.evaluate('window.clicks'), 1)

    def test_user_continue_recovers_after_manually_opening_a_different_item(self):
        self.load(change=False)
        nav = self.adapter.navigator
        self.adapter.next_control().click()
        with self.assertRaises(ManualRequired):
            nav.wait_ready(.1)
        self.page.locator('nav button').nth(2).click()
        self.page.locator('article').evaluate("e=>e.textContent='Manually opened third lesson.'")
        nav.acknowledge()
        nav.ensure_ready()
        self.assertIsNone(nav.pending)
        self.assertEqual(self.page.evaluate('window.clicks'), 2)

    def test_same_url_and_player_have_distinct_identity_on_different_directory_rows(self):
        self.load('<video id="player"></video>')
        old = self.adapter.videos()[0].state.key
        self.page.locator('nav button').nth(1).click()
        self.assertNotEqual(self.adapter.videos()[0].state.key, old)

    def test_static_reading_does_not_swallow_unrecognized_interactive_content(self):
        from activities import CourseActivities
        self.load()
        course = CourseActivities(self.adapter)
        self.assertEqual(course._detect().kind, 'document')
        for control in ('<textarea></textarea>', '<video></video>', '<audio></audio>', '<canvas></canvas>'):
            self.page.locator('article').evaluate('(e,s)=>e.innerHTML=s', control)
            self.assertIsNone(course._detect())

    def test_article_beside_video_or_unknown_question_is_not_a_completed_reading(self):
        from activities import CourseActivities
        for control in ('<video></video>', '<textarea></textarea>', '<div role="textbox"></div>'):
            self.page.set_content('<main><article>Introduction text.</article>'+control+'</main>')
            self.assertIsNone(CourseActivities(self.adapter)._detect())

    def test_reading_page_button_that_does_nothing_is_not_clicked_repeatedly(self):
        from activities import CourseActivities
        self.page.set_content('''<div role="document">Page one text.<button onclick="window.clicks++">Next page</button></div>
            <script>window.clicks=0</script>''')
        course = CourseActivities(self.adapter, document_seconds=3)
        with patch('activities.time') as clock:
            clock.monotonic.side_effect = [0, 3, 6, 9]
            for _ in range(4):
                course.process()
        self.assertEqual(self.page.evaluate('window.clicks'), 1, '翻页无响应时不能再次点击')

    def test_document_waits_after_page_change_and_can_then_advance_again(self):
        from activities import CourseActivities
        self.page.set_content('''<div role="document"><p>Page one.</p><button onclick="window.clicks++;
            this.previousElementSibling.textContent='Page '+window.clicks">Next page</button></div>
            <script>window.clicks=0</script>''')
        course = CourseActivities(self.adapter, document_seconds=3)
        with patch('activities.time') as clock:
            clock.monotonic.side_effect = [0, 3, 6, 9, 11, 12]
            for _ in range(5):
                course.process()
            self.assertEqual(self.page.evaluate('window.clicks'), 1)
            course.process()
        self.assertEqual(self.page.evaluate('window.clicks'), 2)

    def test_long_article_scrolls_the_page_before_it_is_done(self):
        from activities import CourseActivities
        self.page.set_content('<main><article><p>Long reading.</p><div style="height:1800px">More text.</div></article></main>')
        course = CourseActivities(self.adapter, document_seconds=3)
        with patch('activities.time') as clock:
            clock.monotonic.side_effect = [0, 3]
            course.process()
            self.assertEqual(course.process(), 'working')
        self.assertGreater(self.page.evaluate('scrollY'), 100)

    def test_directory_picker_captures_without_executing_course_clicks(self):
        self.page.set_content('''<div id="catalog"><div class="row active"><span>One</span></div>
            <div class="row"><span>Two</span></div><div class="row"><span>Three</span></div></div>
            <main><article>Text.</article></main><script>window.clicks=0;
            document.querySelectorAll('.row').forEach(e=>e.onclick=()=>window.clicks++);</script>''')
        self.assertTrue(hasattr(self.adapter, 'navigator'), '需要网页目录定位器')
        navigator = self.adapter.navigator
        navigator.start_picker()
        self.page.get_by_text('One', exact=True).click()
        self.page.get_by_text('Two', exact=True).click()
        rule = navigator.finish_picker()
        self.assertEqual(self.page.evaluate('window.clicks'), 0)
        self.assertTrue(rule)
        self.assertEqual(navigator.state()['current'], 0)
        self.assertEqual(self.adapter.next_control().inner_text(), 'Two')

    def test_generic_host_change_stops_instead_of_processing_another_site(self):
        self.page.route('https://other.test/**', lambda route: route.fulfill(body='Other'))
        self.page.goto('https://other.test/learn')
        self.assertIn('站点', self.adapter.guard(include_course=False) or '')

    def test_picker_persists_and_keeps_sections_in_same_sidebar(self):
        self.page.set_content('''<aside id="catalog"><section><div class="row active">One</div>
            <div class="row">Two</div></section><section><div class="row">Three</div></section></aside>
            <main><article>Some text.</article></main>''')
        with tempfile.TemporaryDirectory() as folder:
            nav = self.adapter.navigator
            path = Path(folder) / 'navigation.json'
            nav.load(path)
            nav.start_picker()
            self.page.get_by_text('One', exact=True).click()
            self.page.get_by_text('Two', exact=True).click()
            nav.finish_picker()
            restored = Adapter(self.page, 'generic')
            restored.navigator.load(path)
            self.assertEqual(len(restored.navigator.state()['items']), 3)
            self.assertEqual(restored.next_control().inner_text(), 'Two')

    def test_cancelling_directory_picker_restores_normal_clicks(self):
        self.load()
        self.adapter.navigator.start_picker()
        self.adapter.navigator.cancel_picker()
        self.page.locator('nav button').nth(1).click()
        self.assertEqual(self.page.evaluate('window.clicks'), 1)
        self.assertEqual(self.page.locator('#course-helper-locate').count(), 0)

    def custom_sidebar(self):
        self.page.set_content('''<style>.lesson-row {cursor:pointer}</style>
            <main><article>Original lesson.</article><button>Task 1</button></main>
            <div id="sidebar"><div><span>目录</span><span>笔记</span></div>
              <div class="unit"><h3>Section A</h3>
                <div class="lesson-row lesson-row-current"><span>Reading the text</span></div>
                <div class="lesson-row"><span>Reading comprehension</span></div></div>
              <div class="unit"><h3>Section B</h3>
                <div class="lesson-row"><span>Reading the text</span></div></div>
            </div><script>window.clicks=0;
              document.querySelectorAll('.lesson-row').forEach((e,i)=>e.onclick=()=>{
                window.clicks++;
                document.querySelectorAll('.lesson-row').forEach(n=>n.classList.remove('lesson-row-current'));
                e.classList.add('lesson-row-current');
                document.querySelector('article').textContent='Lesson '+i;
              });</script>''')

    def test_custom_labeled_sidebar_is_recognized_across_sections(self):
        self.custom_sidebar()
        nav = self.adapter.navigator
        state = nav.state()
        self.assertIsNotNone(state, '普通 div 侧栏不能因缺少 nav/role 而完全漏识别')
        self.assertEqual(len(state['items']), 3)
        self.assertEqual(self.adapter.next_control().inner_text(), 'Reading comprehension')
        self.adapter.next_control().click()
        nav.wait_ready(2)
        self.assertEqual(nav.state()['current'], 1)
        self.assertEqual(self.adapter.next_control().inner_text(), 'Reading the text')
        self.assertEqual(self.page.evaluate('window.clicks'), 1)

    def test_picker_understands_prefixed_current_and_keeps_custom_sidebar_groups(self):
        self.custom_sidebar()
        nav = self.adapter.navigator
        nav.start_picker()
        self.page.locator('.lesson-row').nth(0).locator('span').click()
        self.page.locator('.lesson-row').nth(1).locator('span').click()
        nav.finish_picker()
        self.assertEqual(len(nav.state()['items']), 3)
        self.assertEqual(self.page.evaluate('window.clicks'), 0)

    def test_picker_can_learn_a_unique_highlight_style_without_an_active_class(self):
        self.page.set_content('''<style>.entry{cursor:pointer}</style>
            <div id="outline"><h3>目录</h3>
              <div class="entry" style="background:rgb(235,245,255)">One</div>
              <div class="entry">Two</div><div class="entry">Three</div></div>
            <main><article>First content.</article></main>''')
        nav = self.adapter.navigator
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'navigation.json'
            nav.load(path)
            nav.start_picker()
            self.page.get_by_text('One', exact=True).click()
            self.page.get_by_text('Two', exact=True).click()
            nav.finish_picker()
            self.page.locator('.entry').nth(0).evaluate("e=>e.removeAttribute('style')")
            self.page.locator('.entry').nth(1).evaluate("e=>e.style.background='rgb(235,245,255)'")
            restored = Adapter(self.page, 'generic')
            restored.navigator.load(path)
            self.assertEqual(restored.navigator.state()['current'], 1)
            self.assertEqual(restored.next_control().inner_text(), 'Three')

    def test_picker_explains_missing_second_click_and_duplicate_row_click(self):
        self.load()
        nav = self.adapter.navigator
        nav.start_picker()
        with self.assertRaisesRegex(ManualRequired, '尚未记录.*点击|0 / 2'):
            nav.finish_picker()
        nav.start_picker()
        self.page.locator('nav button').nth(0).click()
        self.page.locator('nav button').nth(0).click()
        with self.assertRaisesRegex(ManualRequired, '只记录.*当前项|1 / 2'):
            nav.finish_picker()

    def test_picker_blocks_pointer_handlers_before_they_change_the_directory(self):
        self.load()
        self.page.locator('nav').evaluate('''e=>e.addEventListener('pointerdown', ev=>{
            window.clicks++; ev.target.setAttribute('aria-selected','true');
        })''')
        nav = self.adapter.navigator
        nav.start_picker()
        self.page.locator('nav button').nth(0).click()
        self.page.locator('nav button').nth(1).click()
        nav.finish_picker()
        self.assertEqual(self.page.evaluate('window.clicks'), 0)

    def test_non_catalog_active_cards_and_completed_markers_are_not_current_items(self):
        self.page.set_content('''<div><h3>Recent lessons</h3>
          <button class="active-card">A</button><button>B</button></div>
          <div><h3>目录</h3><div class="lesson-row completed">One</div>
          <div class="lesson-row">Two</div></div>''')
        self.assertIsNone(self.adapter.navigator.state())

    def test_picker_does_not_guess_current_when_both_rows_have_same_style(self):
        self.page.set_content('<div id="catalog"><div class="row">One</div><div class="row">Two</div></div>')
        nav = self.adapter.navigator
        nav.start_picker()
        self.page.get_by_text('One', exact=True).click()
        self.page.get_by_text('Two', exact=True).click()
        with self.assertRaisesRegex(ManualRequired, '高亮|当前'):
            nav.finish_picker()

    def test_picker_explains_refresh_instead_of_reporting_missing_clicks(self):
        self.load()
        nav = self.adapter.navigator
        nav.start_picker()
        self.page.locator('nav button').nth(0).click()
        self.page.reload()
        with self.assertRaisesRegex(ManualRequired, '刷新|失效'):
            nav.finish_picker()

    def test_picker_does_not_combine_clicks_from_two_frames(self):
        self.load('<iframe style="height:250px"></iframe>')
        frame = self.page.frames[1]
        frame.set_content('<nav aria-label="课程目录"><button aria-current="step">Other</button><button>Next</button></nav>')
        nav = self.adapter.navigator
        nav.start_picker()
        self.page.locator('nav button').nth(0).click()
        frame.get_by_text('Other', exact=True).click()
        with self.assertRaisesRegex(ManualRequired, '不同.*区域|同一份目录'):
            nav.finish_picker()

    def test_cancel_picker_removes_pointer_interception_too(self):
        self.load()
        self.page.locator('nav').evaluate("e=>e.addEventListener('pointerdown',()=>window.pointerClicks=(window.pointerClicks||0)+1)")
        nav = self.adapter.navigator
        nav.start_picker()
        nav.cancel_picker()
        self.page.locator('nav button').nth(1).click()
        self.assertEqual(self.page.evaluate('window.pointerClicks'), 1)
        self.assertEqual(self.page.evaluate('window.clicks'), 1)

    def test_two_custom_catalogs_are_not_silently_disambiguated(self):
        self.custom_sidebar()
        self.page.locator('body').evaluate("e=>{const other=document.querySelector('#sidebar').cloneNode(true);other.id='other-sidebar';e.append(other)}")
        with self.assertRaisesRegex(ManualRequired, '多个|唯一'):
            self.adapter.navigator.state()

    def test_clickable_section_heading_does_not_replace_leaf_lesson_rows(self):
        self.custom_sidebar()
        self.page.locator('.unit').evaluate_all("nodes=>nodes.forEach(e=>e.style.cursor='pointer')")
        self.assertEqual(self.adapter.next_control().inner_text(), 'Reading comprehension')

    def test_catalog_label_never_expands_to_app_with_active_question_buttons(self):
        self.page.set_content('''<div id="app"><div id="sidebar"><span>目录</span>
              <div class="lesson">One</div><div class="lesson">Two</div></div>
            <div class="exercise"><button class="choice active">Choice A</button>
              <button class="choice">Choice B</button></div></div>''')
        self.assertIsNone(self.adapter.navigator.state())

    def test_idless_sidebar_progress_and_cover_do_not_count_as_body_change(self):
        self.page.set_content('''<style>.entry{cursor:pointer}</style><div class="app">
            <div class="lesson-body">Original body unchanged.</div>
            <div class="sidebar"><h3>目录</h3><img src="data:image/svg+xml,first">
              <div class="entry active">One</div><div class="entry">Two</div>
              <p class="progress">0%</p></div></div><script>
            document.querySelectorAll('.entry').forEach(e=>e.onclick=()=>{
              document.querySelectorAll('.entry').forEach(n=>n.classList.remove('active'));
              e.classList.add('active');
              document.querySelector('.progress').textContent='50%';
              document.querySelector('img').src='data:image/svg+xml,second';
            });</script>''')
        self.adapter.next_control().click()
        with self.assertRaisesRegex(ManualRequired, '正文|播放器'):
            self.adapter.navigator.wait_ready(1)
        self.assertIsNotNone(self.adapter.navigator.pending)

    def test_directory_inside_visible_iframe_is_supported(self):
        self.page.set_content('<iframe title="course" style="width:600px;height:400px"></iframe>')
        frame = self.page.frames[1]
        frame.set_content('<nav aria-label="课程目录"><button aria-current="step">One</button><button>Two</button></nav>')
        self.assertEqual(self.adapter.next_control().inner_text(), 'Two')


if __name__ == '__main__':
    unittest.main()
