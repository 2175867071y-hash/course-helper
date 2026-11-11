"""倍速设置、真实媒体播放与进度判断；全部使用合成页面和本地音频。"""
from dataclasses import replace
import base64
import gc
import io
import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from browser import Adapter
from core import Monitor, Store, VideoState
from main import Task, run_task
from gui import App, automation_values
from gui_backend import Bridge, GuiRequest, execute
from tests.test_background import local_media_source


class SpeedMonitorTests(unittest.TestCase):
    def state(self, current=0, rate=2, **changes):
        state = VideoState('v', current, 20, False, False)
        self.assertTrue(hasattr(state, 'playback_rate'), '媒体状态尚未记录实际倍速')
        return replace(state, playback_rate=rate, **changes)

    def test_double_speed_reaches_natural_end_once(self):
        monitor = Monitor()
        for second in range(10):
            self.assertEqual(monitor.step(self.state(second * 2), second), 'wait')
        self.assertEqual(monitor.step(self.state(20, ended=True, paused=True), 10), 'finished')
        self.assertEqual(monitor.step(self.state(20, ended=True, paused=True), 11), 'wait')

    def test_rate_changes_integrate_instead_of_resetting_progress(self):
        monitor = Monitor()
        for now, current, rate in [(0, 0, 1), (1, 1, 1), (2, 2.5, 2),
                                   (3, 4.5, 2), (4, 6, 1), (5, 7, 1)]:
            self.assertEqual(monitor.step(self.state(current, rate), now), 'wait')
        self.assertEqual(monitor.step(self.state(20, 1, ended=True), 6), 'manual')

    def test_fast_seek_is_still_rejected_at_four_times_speed(self):
        monitor = Monitor()
        monitor.step(self.state(0, 4), 0)
        self.assertEqual(monitor.step(self.state(20, 4, ended=True), 1), 'manual')

    def test_small_jumps_cannot_accumulate_above_actual_rate(self):
        monitor = Monitor()
        actions = [monitor.step(self.state(2.5 * t), t) for t in range(8)]
        self.assertEqual(actions[-1], 'manual')

    def test_focus_resume_supports_actual_speed(self):
        monitor = Monitor()
        monitor.step(self.state(), 0)
        monitor.step(self.state(2), 1)
        self.assertEqual(monitor.step(self.state(2, paused=True), 2), 'manual')
        monitor.resume_from_pause(self.state(6), 4)
        self.assertEqual(monitor.step(self.state(8), 5), 'wait')

    def test_invalid_actual_rates_pause(self):
        for rate in (float('nan'), float('inf'), 0, -1, 99, True, 10**400):
            with self.subTest(rate=rate):
                self.assertEqual(Monitor().step(self.state(rate=rate), 0), 'manual')


class SpeedBrowserTests(unittest.TestCase):
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
        self.page.goto('https://course.example/lesson')
        self.adapter = Adapter(self.page, 'generic')
        self.assertTrue(callable(getattr(self.adapter, 'apply_playback_rate', None)),
                        '播放器尚未支持倍速设置')
        self.adapter.playback_rate = 2
        self.page.set_content('<video muted style="width:320px;height:180px"></video>')
        self.video = self.page.locator('video')
        self.addCleanup(self.page.close)

    def load(self, locator=None, fragment=''):
        locator = locator or self.video
        locator.evaluate('(v,src)=>{v.src=src;v.load()}', local_media_source() + fragment)
        locator.evaluate('v=>v.readyState>=1 ? true : new Promise(r=>v.addEventListener("loadedmetadata",r,{once:true}))')

    def apply(self):
        selected = self.adapter.videos()[0]
        return self.adapter.apply_playback_rate(selected)

    def test_no_speed_menu_plays_real_media_at_double_speed(self):
        self.load()
        before = self.video.evaluate('v=>v.currentTime')
        result = self.apply()
        self.assertEqual(result.state.playback_rate, 2)
        self.assertEqual(self.video.evaluate('v=>v.currentTime'), before)
        self.video.evaluate('v=>v.play()')
        # 音频输出启动阶段尚未稳定推进，等待实际进度后再在同一时钟内测量。
        self.page.wait_for_function('document.querySelector("video").currentTime > 0.5')
        sample = self.video.evaluate('''async v => {
            const before=v.currentTime, start=performance.now();
            await new Promise(resolve=>setTimeout(resolve, 900));
            return {elapsed:(performance.now()-start)/1000, advanced:v.currentTime-before};
        }''')
        self.assertGreater(sample['advanced'], sample['elapsed'] * 1.5)
        self.assertLess(sample['advanced'], sample['elapsed'] * 2.5)

    def test_metadata_lazy_load_does_not_consume_attempt(self):
        self.apply()
        self.assertEqual(self.video.evaluate('v=>v.playbackRate'), 1)
        self.load()
        self.assertEqual(self.apply().state.playback_rate, 2)

    def test_site_reset_is_reported_without_repeated_override(self):
        self.load()
        self.apply()
        self.video.evaluate('v=>v.playbackRate=1')
        with self.assertLogs(level='WARNING') as logs:
            self.assertEqual(self.apply().state.playback_rate, 1)
        self.assertTrue(any('实际' in line for line in logs.output))
        self.apply()
        self.assertEqual(self.video.evaluate('v=>v.playbackRate'), 1)

    def test_throwing_setter_does_not_stop_normal_playback(self):
        self.load()
        self.video.evaluate('''v=>{window.sets=0;Object.defineProperty(v,'playbackRate',{
            get:()=>1,set:()=>{window.sets++;throw Error('unsupported')}})}''')
        with self.assertLogs(level='WARNING'):
            self.assertEqual(self.apply().state.playback_rate, 1)
        self.apply()
        self.assertEqual(self.page.evaluate('window.sets'), 1)

    def test_replaced_element_and_new_source_apply_again(self):
        self.load()
        self.apply()
        self.video.evaluate('v=>v.playbackRate=1')
        self.load(fragment='#next')
        self.assertEqual(self.apply().state.playback_rate, 2)
        self.page.set_content('<video muted style="width:320px;height:180px"></video>')
        self.load()
        self.assertEqual(self.apply().state.playback_rate, 2)

    def test_selected_iframe_video_only_and_no_mutation_during_read(self):
        self.page.set_content('<iframe style="width:500px;height:300px"></iframe><audio></audio>')
        frame = self.page.frames[1]
        frame.set_content('<video muted style="width:320px;height:180px"></video>')
        self.video = frame.locator('video')
        self.load()
        self.adapter.videos()
        self.assertEqual(self.video.evaluate('v=>v.playbackRate'), 1)
        self.assertEqual(self.apply().state.playback_rate, 2)
        self.assertEqual(self.page.locator('audio').evaluate('v=>v.playbackRate'), 1)

    def test_runner_finishes_two_real_media_resources_at_saved_speed(self):
        stream = io.BytesIO()
        with wave.open(stream, 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b'\0\0' * 8000 * 3)
        source = 'data:audio/wav;base64,' + base64.b64encode(stream.getvalue()).decode()
        self.page.set_content('''<video muted style="width:320px;height:180px"></video>
          <button onclick="history.pushState({},'', '/lesson2');
          const v=document.querySelector('video');v.src=window.media+'#second';v.load()">下一节</button>''')
        self.page.evaluate('(src)=>{window.media=src;document.querySelector("video").src=src}', source)
        self.page.wait_for_function('document.querySelector("video").readyState>=1')
        events = []
        control = SimpleNamespace(check=lambda: None,
            sleep=lambda seconds: self.page.wait_for_timeout(seconds * 1000),
            emit=lambda kind, **data: events.append((kind, data)),
            ask=lambda *args: self.fail('正常倍速流程不应转人工：' + str(args)))
        store = Store(':memory:')
        self.addCleanup(store.close)
        outcome = run_task(self.adapter, Task('speed', 'generic', self.page.url, 2),
                           store, .1, 90, control=control)
        self.assertEqual(outcome, 'limit')
        self.assertEqual(self.page.url, 'https://course.example/lesson2')
        self.assertTrue(any(kind == 'progress' and data['completed'] == 2 for kind, data in events))
        self.assertTrue(any('实际 2 倍速' in data.get('message', '') for _, data in events))


class SpeedSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, Path(self.directory.name))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for job in self.app.jobs.values():
            if job.worker:
                job.worker.join(2)
        for callback in self.root.tk.splitlist(self.root.tk.call('after', 'info')):
            self.root.after_cancel(callback)
        self.root.destroy()
        self.app = self.root = None
        gc.collect()
        self.directory.cleanup()

    def require_setting(self):
        self.assertTrue(hasattr(self.app, 'playback_rate'), '界面尚未增加倍速设置')

    def test_setting_persists_and_new_tasks_snapshot_speed(self):
        self.require_setting()
        self.assertEqual(float(self.app.playback_rate.get()), 2)
        received = []
        self.app.runner = lambda request, bridge, directory: received.append(request)
        dialog = self.app.show_settings()
        dialog.playback_rate.set('1.5')
        dialog.save()
        self.app.start('run')
        self.app.worker.join(2)
        self.assertEqual(received[0].playback_rate, 1.5)
        self.app.playback_rate.set('4')
        self.assertEqual(received[0].playback_rate, 1.5)
        self.app._load_settings()
        self.assertEqual(float(self.app.playback_rate.get()), 1.5)

    def test_invalid_settings_cannot_launch_or_save(self):
        self.require_setting()
        for value in ('nan', 'inf', '0', '4.1', 'text'):
            self.app.playback_rate.set(value)
            with self.assertRaises(ValueError):
                automation_values(self.app)

    def test_old_and_invalid_saved_settings_use_default(self):
        self.require_setting()
        for saved in ({'auto_quiz': False}, {'playback_rate': 999},
                      {'playback_rate': True}, {'playback_rate': 10**400}):
            (Path(self.directory.name) / 'settings.json').write_text(json.dumps(saved), encoding='utf-8')
            self.app._load_settings()
            self.assertEqual(float(self.app.playback_rate.get()), 2)

    def test_backend_rejects_invalid_rate_before_browser_launch(self):
        request = GuiRequest('run', 'generic', 'example')
        self.assertTrue(hasattr(request, 'playback_rate'), '任务尚未携带倍速设置')
        for rate in (0, 9, float('nan'), True, '2', 10**400):
            request.playback_rate = rate
            bridge = Bridge()
            with patch('playwright.sync_api.sync_playwright') as launch:
                execute(request, bridge, Path(self.directory.name))
            launch.assert_not_called()
            messages = []
            while not bridge.events.empty():
                messages.append(bridge.events.get_nowait())
            self.assertEqual(messages[-1]['outcome'], 'error')
