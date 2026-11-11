"""真实 Edge 上验证截图作答、二次确认、结果确认与下一节衔接。"""
import html
import time
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from activities import CourseActivities
from browser import Adapter, ManualRequired
from tests.test_quiz_dom import OfflineAI


class AutomaticSubmissionTests(unittest.TestCase):
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
        self.page.route('https://course.example/**', lambda route: route.fulfill(
            content_type='text/html', body='<main></main>'))
        self.page.goto('https://course.example/lesson1')
        self.adapter = Adapter(self.page, 'generic')
        self.course = CourseActivities(self.adapter, ai_base_url='https://ai.example/v1',
            ai_api_key='test-key', vision_model='vision-model', screenshot_quiz=True)
        self.ai = OfflineAI([{'id': '0', 'selected': [1], 'confidence': .99}])
        self.network = patch('urllib.request.build_opener', return_value=self.ai)
        self.network.start()

    def tearDown(self):
        self.network.stop()
        # 未识别的原生弹窗按设计留给用户；测试关闭页面释放它。
        self.page.close()

    def load(self, action, dialog='', frame=None):
        frame = frame or self.page.main_frame
        frame.set_content('''<main><form onsubmit="event.preventDefault();window.starts++;begin()">
          <fieldset><legend>Choose the even number</legend>
          <label><input type=radio name=q>Three</label>
          <label><input type=radio name=q>Two</label></fieldset>
          <button>提交</button></form><output id=result></output></main>'''
          + dialog + '''<script>window.starts=0;window.confirms=0;window.sent=0;
          function finish(){window.sent++;document.querySelector('#result').textContent='提交成功'}
          function begin(){''' + action + '}</script>')
        return frame

    def modal(self, message='确定提交本次测验吗？', action='finish();this.closest("dialog").close()'):
        return ('<dialog id=confirmation><p>' + message + '</p>'
                '<button type=button onclick="' + html.escape('window.confirms++;' + action, quote=True) + '">确定</button>'
                '<button type=button onclick="this.closest(\'dialog\').close()">取消</button></dialog>')

    def layer(self, message='确定提交本次测验吗？',
              action='finish();document.querySelector(".mask").style.display="none"', buttons=None):
        # 模拟超星/雨课堂常见的自定义确认层：不是 dialog、没有 role=dialog。
        if buttons is None:
            buttons = ('<a class="ok" onclick="' +
                       html.escape('window.confirms++;' + action, quote=True) + '">确定</a>'
                       '<a class="no">取消</a>')
        return ('<div class="mask" style="display:none"><p>' + message + '</p>'
                + buttons + '</div>')

    def complete(self):
        try:
            result = self.course.process()
            for _ in range(30):
                if result == 'done':
                    return
                self.page.wait_for_timeout(30)
                result = self.course.process()
        except ManualRequired as exc:
            self.fail('正常交卷流程不应要求人工处理：' + str(exc))
        self.fail('未确认提交成功')

    def test_native_confirm_submits_and_next_quiz_can_run_without_prompt(self):
        self.load("if(confirm('确定提交本次测验吗？')) finish()")
        self.complete()
        self.assertEqual(self.page.evaluate('[starts,sent]'), [1, 1])
        self.assertFalse(self.adapter.dialog_pending)
        self.page.locator('main').evaluate("e=>e.insertAdjacentHTML('afterend','<a href=\"/lesson2\">下一节</a>')")
        self.page.route('https://course.example/lesson2', lambda route: route.fulfill(
            content_type='text/html', body='<main><article>Next lesson reading material</article></main>'))
        self.adapter.advance_activity(self.course.identity(), timeout=3)
        self.assertEqual(self.page.url, 'https://course.example/lesson2')
        self.load("if(confirm('Are you sure you want to submit your answers?')) finish()")
        self.complete()
        self.assertEqual(self.page.evaluate('[starts,sent]'), [1, 1])
        self.assertEqual(len(self.ai.requests), 2)

    def test_delayed_html_confirmation_is_clicked_once_then_result_is_required(self):
        self.load("setTimeout(()=>document.querySelector('dialog').showModal(),150)", self.modal())
        self.complete()
        for _ in range(3):
            self.assertEqual(self.course.process(), 'done')
        self.assertEqual(self.page.evaluate('[starts,confirms,sent]'), [1, 1, 1])
        self.assertEqual(len(self.ai.requests), 1)

    def test_confirmation_in_parent_of_nested_quiz_is_handled(self):
        self.page.set_content('<iframe style="width:1000px;height:600px"></iframe>' + self.modal(action="document.querySelector('iframe').contentWindow.finish();this.closest('dialog').close()")
            + '<script>window.confirms=0</script>')
        frame = self.load("parent.document.querySelector('dialog').showModal()", frame=self.page.frames[1])
        self.complete()
        self.assertEqual(self.page.evaluate('confirms'), 1)
        self.assertEqual(frame.evaluate('[starts,sent]'), [1, 1])

    def test_confirmation_that_does_not_respond_is_not_repeated(self):
        self.load("document.querySelector('dialog').showModal()", self.modal(action=''))
        self.course.process()
        for _ in range(4):
            self.course.process()
        self.assertEqual(self.page.evaluate('[starts,confirms,sent]'), [1, 1, 0])
        self.course.submitted[self.course.pending_quiz.key] = time.monotonic() - 31
        with self.assertRaisesRegex(ManualRequired, '提交'):
            self.course.process()
        self.assertEqual(self.page.evaluate('confirms'), 1)

    def test_old_parent_dialog_does_not_block_new_child_confirmation(self):
        self.page.set_content('<iframe style="width:900px;height:500px"></iframe>' + self.modal()
            + '<script>window.confirms=0</script>')
        self.page.locator('dialog').evaluate('e=>{e.show();e.style.cssText="position:fixed;left:1000px;top:0;margin:0;width:150px"}')
        frame = self.load("document.querySelector('dialog').showModal()", self.modal(), frame=self.page.frames[1])
        self.complete()
        self.assertEqual(self.page.evaluate('confirms'), 0)
        self.assertEqual(frame.evaluate('[starts,confirms,sent]'), [1, 1, 1])

    def test_missing_answers_warning_is_not_confirmed(self):
        self.load("document.querySelector('dialog').showModal()", self.modal('还有 1 道题未作答，确定提交吗？'))
        self.course.process()
        self.course.process()
        self.assertEqual(self.page.evaluate('[starts,confirms,sent]'), [1, 0, 0])
        self.assertTrue(self.page.locator('dialog').is_visible())

    def test_existing_confirmation_is_not_reused_for_this_submission(self):
        self.load('', self.modal())
        self.page.locator('dialog').evaluate('e=>{e.show();e.style.top="250px"}')
        self.course.process()
        self.course.process()
        self.assertEqual(self.page.evaluate('[starts,confirms,sent]'), [1, 0, 0])

    def test_manual_review_setting_still_stops_before_submission(self):
        self.course.quiz_submit = False
        self.load("document.querySelector('dialog').showModal()", self.modal())
        with self.assertRaisesRegex(ManualRequired, '手动提交'):
            self.course.process()
        self.assertEqual(self.page.evaluate('[starts,confirms,sent]'), [0, 0, 0])
        self.assertTrue(self.page.locator('input').nth(1).is_checked())

    def test_unrelated_native_confirmation_is_not_accepted(self):
        self.load("if(confirm('确定删除已提交的测验吗？')) finish()")
        # 测试模拟用户取消未处理弹窗，避免阻塞真实浏览器；程序若误接收则 sent 会变为 1。
        self.page.on('dialog', lambda dialog: dialog.dismiss() if self.adapter.dialog_pending else None)
        self.course.process()
        self.assertTrue(self.adapter.dialog_pending)
        self.assertEqual(self.page.evaluate('[starts,sent]'), [1, 0])

    def test_expired_submission_does_not_confirm_a_late_dialog(self):
        self.load("document.querySelector('dialog').showModal()", self.modal())
        self.course.process()
        self.course.submitted[self.course.pending_quiz.key] = time.monotonic() - 31
        with self.assertRaisesRegex(ManualRequired, '提交'):
            self.course.process()
        self.assertEqual(self.page.evaluate('[starts,confirms,sent]'), [1, 0, 0])

    def test_manual_submit_then_success_prompt_is_recognised(self):
        # 用户手动点“提交”后页面出现成功提示，指纹会变；程序仍应认出这份卷子已交并返回 done。
        self.load("document.querySelector('.mask').style.display='block'",
                  self.layer('还有 1 道题未作答，确定提交吗？'))
        self.course.process()
        self.page.evaluate("document.querySelector('.mask').style.display='none';finish()")
        self.assertEqual(self.course.process(), 'done')
        self.assertEqual(self.page.locator('output').inner_text(), '提交成功')
        self.assertTrue(self.course.advance_key())

    def test_submission_that_removes_quiz_area_is_treated_as_done(self):
        # 提交后平台跳到结果页、题目区域消失时，应进入下一节而不是一直等题目。
        self.load("document.querySelector('.mask').style.display='block'", self.layer())
        self.course.process()
        self.page.main_frame.set_content('<main><p>提交成功，感谢作答</p></main>')
        pending = self.course.pending_quiz
        self.course.submitted[pending.key] = time.monotonic() - 6
        self.assertEqual(self.course.process(), 'done')
        self.assertTrue(self.course.advance_key())

    def test_blank_after_submit_waits_for_receipt_without_resubmitting(self):
        self.load("document.querySelector('form').remove()")
        self.assertEqual(self.course.process(), 'working')
        pending = self.course.pending_quiz
        self.course.submitted[pending.key] = time.monotonic() - 6
        self.assertEqual(self.course.process(), 'working')
        self.course.submitted[pending.key] = time.monotonic() - 31
        with self.assertRaisesRegex(ManualRequired, '提交'):
            self.course.process()
        self.page.locator('#result').evaluate("e=>e.textContent='提交成功'")
        self.assertEqual(self.course.process(), 'done')
        self.assertEqual(self.page.evaluate('starts'), 1)
        self.assertEqual(len(self.ai.requests), 1)

    def test_graded_answer_in_quiz_pane_confirms_submission(self):
        self.load("document.querySelector('main').replaceChildren()")
        self.assertEqual(self.course.process(), 'working')
        pending = self.course.pending_quiz
        self.course.submitted[pending.key] = time.monotonic() - 6
        row = '<div><b>我的答案：</b> B <span>20.0 分</span></div>'
        self.page.locator('main').evaluate("(e,h)=>e.innerHTML='<aside>'+h+'</aside>'", row)
        self.assertEqual(self.course.process(), 'working')
        self.page.locator('main').evaluate("(e,h)=>e.innerHTML=h", row)
        self.assertEqual(self.course.process(), 'done')
        self.assertEqual(self.page.evaluate('starts'), 1)

    def test_div_style_confirmation_is_clicked_once(self):
        # 超星新版用 div/span 做按钮，没有 button 标签，也没有 onclick 属性。
        layer = ('<div class="mask" style="display:none"><p>确认提交？</p>'
                 '<div class="btn cancel">取消</div><div class="btn confirm">提交</div></div>')
        self.load("document.querySelector('.mask').style.display='block'", layer)
        self.page.locator('.confirm').evaluate(
            "e=>e.addEventListener('click',()=>{window.confirms++;"
            "e.closest('.mask').style.display='none';finish()})")
        self.complete()
        self.assertEqual(self.page.evaluate('[starts,confirms,sent]'), [1, 1, 1])

    def test_custom_confirmation_layer_is_clicked_once(self):
        # 超星等页面用普通 div 做确认层，既不是 dialog 也没有 role=dialog。
        self.load("document.querySelector('.mask').style.display='block'", self.layer())
        self.complete()
        self.assertEqual(self.page.evaluate('[starts,confirms,sent]'), [1, 1, 1])
        self.assertEqual(len(self.ai.requests), 1)

    def test_custom_layer_with_missing_answers_is_not_confirmed(self):
        self.load("document.querySelector('.mask').style.display='block'",
                  self.layer('还有 1 道题未作答，确定提交吗？'))
        self.course.process()
        self.course.process()
        self.assertEqual(self.page.evaluate('[starts,confirms,sent]'), [1, 0, 0])
        self.assertTrue(self.page.locator('.mask').is_visible())

    def test_custom_layer_with_two_confirm_buttons_is_not_clicked(self):
        buttons = '<a class="ok">确定</a><a class="ok2">确定</a>'
        self.load("document.querySelector('.mask').style.display='block'", self.layer(buttons=buttons))
        self.course.process()
        self.course.process()
        self.assertEqual(self.page.evaluate('[starts,confirms,sent]'), [1, 0, 0])

    def test_custom_layer_in_parent_frame_is_clicked_once(self):
        # 自定义确认层放在课件父页面，且不是 dialog。
        parent_layer = self.layer(
            action="document.querySelector('iframe').contentWindow.finish();"
                   "document.querySelector('.mask').style.display='none'")
        self.page.set_content('<iframe style="width:1000px;height:600px"></iframe>' + parent_layer
                              + '<script>window.confirms=0;window.starts=0;window.sent=0</script>')
        frame = self.load("parent.document.querySelector('.mask').style.display='block'",
                          frame=self.page.frames[1])
        self.complete()
        self.assertEqual(self.page.evaluate('confirms'), 1)
        self.assertEqual(frame.evaluate('[starts,sent]'), [1, 1])

    def test_success_prompt_is_dismissed_before_next_lesson(self):
        prompt = ('<div class="done" style="display:none"><p>提交成功</p>'
                  '<a onclick="window.dismissed=1;document.querySelector(\'.done\').style.display=\'none\'">确定</a></div>')
        self.load("if(confirm('确定提交本次测验吗？')) {"
                  "finish();document.querySelector('.done').style.display='block'}", prompt)
        self.complete()
        self.assertEqual(self.page.evaluate('window.dismissed||0'), 1)
        self.assertEqual(self.page.evaluate('window.sent'), 1)


class ActivityTabTests(unittest.TestCase):
    """同一节内的“1 视频 / 2 章节测验”选项卡。"""

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
        self.addCleanup(self.page.close)

    def adapter(self, identity):
        adapter = Adapter(self.page, 'chaoxing')
        adapter.activity_identity = identity
        return adapter

    def test_next_activity_tab_is_clicked_once(self):
        self.page.set_content('''<div class="tabs">
            <a class="tab active">1 视频</a><a class="tab">2 章节测验</a></div><main>video</main>''')
        self.page.locator('.tab').nth(1).evaluate(
            "e=>e.addEventListener('click',()=>{document.querySelectorAll('.tab')"
            ".forEach(n=>n.classList.remove('active'));e.classList.add('active');"
            "document.querySelector('main').textContent='quiz'})")
        adapter = self.adapter(lambda: self.page.locator('main').inner_text())
        self.assertTrue(adapter.switch_activity_tab('video', timeout=3))
        self.assertEqual(self.page.locator('main').inner_text(), 'quiz')
        self.assertEqual(self.page.locator('[data-ch-tab]').count(), 0)

    def test_active_class_transfer_counts_as_switched(self):
        # 答题区还在加载、活动键没变时，选项卡高亮转移本身就说明切换成功。
        self.page.set_content('''<div class="tabs">
            <a class="tab active">1 视频</a><a class="tab">2 章节测验</a></div><main>video</main>''')
        self.page.locator('.tab').nth(1).evaluate(
            "e=>e.addEventListener('click',()=>{document.querySelectorAll('.tab')"
            ".forEach(n=>n.classList.remove('active'));e.classList.add('active')})")
        adapter = self.adapter(lambda: 'video')
        self.assertTrue(adapter.switch_activity_tab('video', timeout=3))
        self.assertEqual(self.page.locator('[data-ch-tab]').count(), 0)

    def test_new_tab_is_reported_instead_of_retrying(self):
        self.page.set_content('''<div class="tabs">
            <a class="tab active">1 视频</a><a class="tab">2 章节测验</a></div><main>video</main>''')
        self.page.locator('.tab').nth(1).evaluate(
            "e=>e.addEventListener('click',()=>window.open('about:blank'))")
        adapter = self.adapter(lambda: 'video')
        with self.assertRaisesRegex(ManualRequired, '新标签页'):
            adapter.switch_activity_tab('video', timeout=3)
        self.assertEqual(self.page.locator('[data-ch-tab]').count(), 0)

    def test_nested_tab_markup_counts_once_and_clicks_next(self):
        # 真实结构里 li 内还有 a/span；旧版会把同一项算两次，结果点回当前项。
        self.page.set_content('''<ul class="tabs">
            <li class="active"><a href="#"><span>1</span>视频</a></li>
            <li><a href="#"><span>2</span>章节测验</a></li></ul><main>video</main>''')
        self.page.locator('li').nth(1).evaluate(
            "e=>e.addEventListener('click',()=>{document.querySelectorAll('li')"
            ".forEach(n=>n.classList.remove('active'));e.classList.add('active');"
            "document.querySelector('main').textContent='quiz'})")
        adapter = self.adapter(lambda: self.page.locator('main').inner_text())
        self.assertTrue(adapter.switch_activity_tab('video', timeout=3))
        self.assertEqual(self.page.locator('main').inner_text(), 'quiz')

    def test_tab_order_follows_number_prefix_not_dom_order(self):
        # DOM 里“2 章节测验”排在前面，视觉上靠数字前缀显示为第二项；应按数字顺序点下一项。
        self.page.set_content('''<div class="tabs">
            <a class="tab">2 章节测验</a><a class="tab active">1 视频</a></div><main>video</main>''')
        self.page.locator('.tab').nth(0).evaluate(
            "e=>e.addEventListener('click',()=>{document.querySelectorAll('.tab')"
            ".forEach(n=>n.classList.remove('active'));e.classList.add('active');"
            "document.querySelector('main').textContent='quiz'})")
        adapter = self.adapter(lambda: self.page.locator('main').inner_text())
        self.assertTrue(adapter.switch_activity_tab('video', timeout=3))
        self.assertEqual(self.page.locator('main').inner_text(), 'quiz')

    def test_no_activity_tab_returns_none(self):
        self.page.set_content('<main>video</main>')
        adapter = self.adapter(lambda: 'video')
        self.assertIsNone(adapter.switch_activity_tab('video', timeout=1))

    def test_ambiguous_tab_groups_are_not_clicked(self):
        self.page.set_content('''<div><a class="t active">1 视频</a><a class="t">2 测验</a></div>
            <div><a class="u active">1 文档</a><a class="u">2 测验</a></div>''')
        adapter = self.adapter(lambda: 'x')
        with self.assertRaisesRegex(ManualRequired, '多组'):
            adapter.switch_activity_tab('video', timeout=1)
        self.assertEqual(self.page.locator('[data-ch-tab]').count(), 0)


if __name__ == '__main__':
    unittest.main()
