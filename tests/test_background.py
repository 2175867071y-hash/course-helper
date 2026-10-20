"""本地课件中的失焦与鼠标离开处理；不修改课程服务器进度。"""
import base64
import io
import unittest
import wave
from unittest.mock import Mock

from playwright.sync_api import sync_playwright


def nested_course(context, origin='https://mooc1.chaoxing.com'):
    """复现已观察到的双层课件：子播放器给 top 绑定 mouseout。"""
    def respond(route):
        path = route.request.url.split(origin, 1)[-1]
        if path == '/cards':
            html = '<iframe src="/player" style="width:400px;height:250px"></iframe>'
        elif path == '/player':
            html = '''
              <div class="video-js"><video controls muted style="width:320px;height:180px"></video>
                <button id="pause">暂停</button></div>
              <script>
                const video=document.querySelector('video');
                top.addEventListener('mouseout',event=>{
                  top.outs++;
                  if (!(event.relatedTarget || event.toElement)) {
                    video.pause(); top.pauses++;
                  }
                });
                document.querySelector('#pause').onclick=()=>video.pause();
              </script>'''
        else:
            html = '''
              <iframe src="/cards" style="width:450px;height:300px"></iframe>
              <aside>目录<button id="next">下一节</button></aside>
              <input id="one"><input id="two">
              <script>
                window.outs=0;window.pauses=0;window.localOuts=0;window.nexts=0;window.blurs=0;
                document.querySelector('aside').addEventListener('mouseout',()=>localOuts++);
                document.querySelector('#next').onclick=()=>nexts++;
                document.querySelector('#one').onblur=()=>blurs++;
              </script>'''
        route.fulfill(body=html)
    context.route(origin + '/**', respond)
    page = context.new_page()
    page.goto(origin + '/mycourse/studentstudy')
    player = page.frame_locator('iframe').frame_locator('iframe')
    player.locator('video').wait_for()
    return page, player


def local_media_source():
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b'\0\0' * 8000 * 30)
    return 'data:audio/wav;base64,' + base64.b64encode(stream.getvalue()).decode()


class BackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='msedge', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()

    def tearDown(self):
        self.context.close()

    def install(self, enabled=True):
        import browser
        self.assertTrue(callable(getattr(browser, 'install_background_playback', None)),
                        '后台播放兼容尚未实现')
        return browser.install_background_playback(self.context, enabled)

    def load(self):
        self.context.route('https://course.example/**', lambda route: route.fulfill(body='''
          <div class="video-js"><video controls></video><button id="pause">暂停</button></div>
          <input id="one"><input id="two"><aside><button id="next">下一节</button></aside>
          <script>
            window.events={blur:0,visibility:0,leave:0,input:0,next:0,pause:0,other:0};
            window.addEventListener('blur',e=>{if(e.target===window)events.blur++},true);
            document.addEventListener('visibilitychange',()=>events.visibility++,true);
            document.querySelector('video').addEventListener('mouseleave',()=>events.leave++);
            document.querySelector('.video-js').addEventListener('mouseout',()=>events.leave++);
            document.querySelector('#one').addEventListener('blur',()=>events.input++);
            document.querySelector('#pause').onclick=()=>events.pause++;
            document.querySelector('#next').onclick=()=>events.next++;
            document.querySelector('aside').addEventListener('mouseleave',()=>events.other++);
          </script>'''))
        page = self.context.new_page()
        page.goto('https://course.example/lesson')
        return page

    def test_background_events_blocked_but_input_navigation_and_pause_work(self):
        self.install()
        page = self.load()
        page.evaluate('''() => {
          window.dispatchEvent(new Event('blur'));
          Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});
          document.dispatchEvent(new Event('visibilitychange'));
          document.querySelector('video').dispatchEvent(new MouseEvent('mouseleave'));
          document.querySelector('.video-js').dispatchEvent(new MouseEvent('mouseout', {bubbles:true}));
          document.querySelector('#one').dispatchEvent(new FocusEvent('blur'));
          document.querySelector('#next').click(); document.querySelector('#pause').click();
          document.querySelector('aside').dispatchEvent(new MouseEvent('mouseleave'));
        }''')
        self.assertEqual(page.evaluate('events'), dict(blur=0, visibility=0, leave=0,
                                                     input=1, next=1, pause=1, other=1))

    def test_disabled_preserves_page_events(self):
        self.install(False)
        page = self.load()
        page.evaluate("window.dispatchEvent(new Event('blur'))")
        self.assertEqual(page.evaluate('events.blur'), 1)

    def test_current_pages_reload_popup_and_nested_frame_are_covered(self):
        existing = self.context.new_page()
        self.install()
        existing.evaluate("window.count=0;addEventListener('blur',()=>count++);dispatchEvent(new Event('blur'))")
        self.assertEqual(existing.evaluate('count'), 0)
        page = self.load()
        page.reload()
        page.evaluate("window.dispatchEvent(new Event('blur'))")
        self.assertEqual(page.evaluate('events.blur'), 0)
        page.evaluate("document.body.insertAdjacentHTML('beforeend','<iframe src=\"https://course.example/frame\"></iframe>')")
        frame = page.frame_locator('iframe')
        frame.locator('video').wait_for()
        self.assertEqual(frame.locator('body').evaluate("() => {dispatchEvent(new Event('blur'));return events.blur}"), 0)
        with page.expect_popup() as popup_info:
            page.evaluate("window.open('https://course.example/popup')")
        popup = popup_info.value
        popup.wait_for_load_state()
        popup.evaluate("window.dispatchEvent(new Event('blur'))")
        self.assertEqual(popup.evaluate('events.blur'), 0)

    def test_pointer_transitions_inside_controls_are_preserved(self):
        self.install()
        page = self.load()
        page.evaluate('''() => {
          const button=document.querySelector('#pause');
          button.dispatchEvent(new MouseEvent('mouseout', {bubbles:true, relatedTarget:document.querySelector('video')}));
        }''')
        self.assertEqual(page.evaluate('events.leave'), 1)

    def test_unknown_video_parent_does_not_capture_sibling_directory(self):
        self.install()
        self.context.route('https://layout.example/**', lambda route: route.fulfill(body=
            '<div class="lesson"><video></video><aside>目录</aside></div><footer>底部</footer>'))
        page = self.context.new_page()
        page.goto('https://layout.example/lesson')
        page.evaluate('''() => {
          window.leaves=0;
          const aside=document.querySelector('aside');
          aside.addEventListener('mouseleave',()=>leaves++);
          aside.dispatchEvent(new MouseEvent('mouseleave',{relatedTarget:document.querySelector('footer')}));
        }''')
        self.assertEqual(page.evaluate('leaves'), 1)

    def test_setup_failure_is_reported_without_raw_error_or_silent_success(self):
        import browser
        self.assertTrue(callable(getattr(browser, 'install_background_playback', None)))
        broken = Mock()
        broken.add_init_script.side_effect = RuntimeError('secret-in-driver-error')
        with self.assertLogs(level='WARNING') as recorded:
            self.assertFalse(browser.install_background_playback(broken, True))
        self.assertNotIn('secret-in-driver-error', ' '.join(recorded.output))

    def test_preloaded_page_requires_reload_for_earlier_capture_listeners(self):
        page = self.load()
        with self.assertLogs(level='WARNING') as recorded:
            self.assertFalse(self.install())
        self.assertIn('刷新', ' '.join(recorded.output))
        page.evaluate("window.dispatchEvent(new Event('blur'))")
        self.assertEqual(page.evaluate('events.blur'), 1)
        page.reload()
        page.evaluate("window.dispatchEvent(new Event('blur'))")
        self.assertEqual(page.evaluate('events.blur'), 0)

    def test_top_exit_with_nested_video_preserves_directory_and_form_handlers(self):
        self.install()
        page, _ = nested_course(self.context)
        page.evaluate('''() => {
          const aside=document.querySelector('aside');
          aside.dispatchEvent(new MouseEvent('mouseout',{bubbles:true,relatedTarget:null}));
          document.querySelector('#next').click();
          document.querySelector('#one').dispatchEvent(new FocusEvent('blur'));
        }''')
        self.assertEqual(page.evaluate('({outs,pauses,localOuts,nexts,blurs})'),
                         dict(outs=0, pauses=0, localOuts=1, nexts=1, blurs=1))
        page.evaluate('''document.querySelector('aside').dispatchEvent(
          new MouseEvent('mouseout',{bubbles:true,relatedTarget:document.querySelector('#next')}))''')
        self.assertEqual(page.evaluate('outs'), 1, '页内移动仍交给站点处理')

    def test_top_exit_guard_is_scoped_to_visible_chaoxing_course(self):
        self.install()
        page, _ = nested_course(self.context, 'https://course.example')
        page.evaluate("document.body.dispatchEvent(new MouseEvent('mouseout',{bubbles:true}))")
        self.assertEqual(page.evaluate('pauses'), 1, '未知站点的窗口事件不可被接管')
        page, _ = nested_course(self.context)
        page.locator('iframe').evaluate("el=>el.style.display='none'")
        page.evaluate("document.body.dispatchEvent(new MouseEvent('mouseout',{bubbles:true}))")
        self.assertEqual(page.evaluate('pauses'), 1, '隐藏课件不应影响当前页')

    def test_zero_size_iframe_and_video_do_not_capture_top_exit(self):
        self.install()
        for target in ('iframe', 'video'):
            with self.subTest(target=target):
                page, player = nested_course(self.context)
                element = page.locator('iframe') if target == 'iframe' else player.locator('video')
                size = element.evaluate('''el=>{
                  el.style.cssText='width:0;height:0;border:0;padding:0';
                  const rect=el.getBoundingClientRect();
                  return [rect.width,rect.height];
                }''')
                self.assertEqual(size, [0, 0])
                page.evaluate("document.body.dispatchEvent(new MouseEvent('mouseout',{bubbles:true}))")
                self.assertEqual(page.evaluate('pauses'), 1, '零尺寸课件不能接管窗口事件')
                page.close()


class MinimizedPlaybackTests(unittest.TestCase):
    def test_nested_player_top_window_exit_then_minimize_and_manual_pause(self):
        from browser import install_background_playback
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=False)
            try:
                for enabled in (False, True):
                    with self.subTest(background=enabled):
                        context = browser.new_context()
                        try:
                            install_background_playback(context, enabled)
                            page, player = nested_course(context)
                            video = player.locator('video')
                            video.evaluate('(v,src)=>{v.src=src}', local_media_source())
                            video.evaluate('v=>v.play()')
                            page.mouse.move(700, 500)
                            start = video.evaluate('v=>v.currentTime')
                            # 真实浏览器输入：移出页面边界，使 relatedTarget 为 null。
                            page.mouse.move(-10, -10)
                            session = context.new_cdp_session(page)
                            window = session.send('Browser.getWindowForTarget')['windowId']
                            session.send('Browser.setWindowBounds', {'windowId': window,
                                'bounds': {'windowState': 'minimized'}})
                            page.wait_for_timeout(1600)
                            state = video.evaluate('v=>({paused:v.paused,time:v.currentTime,rate:v.playbackRate})')
                            self.assertEqual(session.send('Browser.getWindowBounds',
                                {'windowId': window})['bounds']['windowState'], 'minimized')
                            self.assertEqual(state['rate'], 1)
                            if enabled:
                                self.assertFalse(state['paused'])
                                self.assertGreater(state['time'] - start, .8)
                                player.locator('#pause').click()
                                self.assertTrue(video.evaluate('v=>v.paused'))
                            else:
                                self.assertTrue(state['paused'])
                                self.assertGreater(page.evaluate('pauses'), 0)
                        finally:
                            context.close()
            finally:
                browser.close()

    def test_real_media_keeps_playing_in_minimized_edge_after_pointer_leaves(self):
        from browser import install_background_playback
        stream = io.BytesIO()
        with wave.open(stream, 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b'\0\0' * 8000 * 30)
        source = 'data:audio/wav;base64,' + base64.b64encode(stream.getvalue()).decode()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=False)
            try:
                for enabled in (False, True):
                    with self.subTest(background=enabled):
                        context = browser.new_context()
                        try:
                            install_background_playback(context, enabled)
                            context.route('https://course.example/**', lambda route: route.fulfill(body='''
                              <div class="video-js" style="width:320px;height:200px">
                                <video controls muted style="width:320px;height:200px"></video></div>
                              <script>
                                const video=document.querySelector('video');
                                video.addEventListener('mouseleave',()=>video.pause());
                                setInterval(()=>{if(document.hidden||!document.hasFocus())video.pause()},100);
                              </script>'''))
                            page = context.new_page()
                            page.goto('https://course.example/video')
                            video = page.locator('video')
                            video.evaluate('(v,src)=>{v.src=src}', source)
                            page.wait_for_function('document.querySelector("video").readyState>=2')
                            session = context.new_cdp_session(page)
                            window = session.send('Browser.getWindowForTarget')['windowId']
                            session.send('Browser.setWindowBounds', {'windowId':window,
                                'bounds':{'windowState':'minimized'}})
                            page.mouse.move(80, 80)
                            video.evaluate('v=>v.play()')
                            page.mouse.move(600, 500)
                            start = video.evaluate('v=>v.currentTime')
                            page.wait_for_timeout(1600)
                            state = video.evaluate('v=>({paused:v.paused,time:v.currentTime,rate:v.playbackRate})')
                            bounds = session.send('Browser.getWindowBounds', {'windowId':window})['bounds']
                            self.assertEqual(bounds['windowState'], 'minimized')
                            self.assertEqual(state['rate'], 1)
                            if enabled:
                                self.assertFalse(state['paused'])
                                self.assertGreater(state['time'] - start, .8)
                            else:
                                self.assertTrue(state['paused'], '对照组须复现离开播放器暂停')
                                self.assertLess(state['time'] - start, .2)
                        finally:
                            context.close()
            finally:
                browser.close()

