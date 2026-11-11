"""在隔离的 Edge 配置中使用真正的站点缩放，核对拼图的实际像素。"""
import base64
import json
import math
from pathlib import Path
import struct
import tempfile
import unittest

from playwright.sync_api import sync_playwright
from quiz_capture import capture_region


class SiteZoomTests(unittest.TestCase):
    def test_full_question_at_native_browser_zoom(self):
        with sync_playwright() as pw:
            for zoom, dpi in ((.8, 1), (.8, 1.25), (1.25, 1.5)):
                with self.subTest(zoom=zoom, dpi=dpi), tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                    pref = Path(tmp, 'Default', 'Preferences')
                    pref.parent.mkdir()
                    pref.write_text(json.dumps({'partition': {'per_host_zoom_levels': {
                        'x': {'localhost': {'zoom_level': math.log(zoom, 1.2),
                                             'last_modified': '13437902884788061'}}}}}))
                    ctx = pw.chromium.launch_persistent_context(tmp, channel='msedge', headless=True,
                        no_viewport=True, args=['--window-size=1150,870', f'--force-device-scale-factor={dpi}'])
                    try:
                        page = ctx.pages[0]
                        page.route('http://localhost/**', lambda r: r.fulfill(content_type='text/html',
                            body='<div style="height:1190px"></div><iframe style="margin-left:30px;'
                                 'width:765px;height:440px"></iframe><footer style="height:500px"></footer>'))
                        page.goto('http://localhost/')
                        cdp = ctx.new_cdp_session(page)
                        self.assertAlmostEqual(cdp.send('Page.getLayoutMetrics')['cssVisualViewport']['zoom'], zoom)
                        cdp.detach()
                        frame = page.frames[1]
                        frame.set_content('<main style="height:320px;overflow:auto"><article style="width:700px">' +
                            ''.join(f'<div style="height:140px;display:flex;background:rgb({k*20},80,120)">'
                                    '<i style="margin-left:auto;width:45px;background:rgb(5,175,50)"></i></div>'
                                    for k in range(10)) + '</article></main>')
                        png = capture_region(frame.locator('article'))
                        self.assertGreater(page.evaluate('scrollY'), 0)
                        self.assertEqual(struct.unpack('>II', png[16:24]), (700, 1400))
                        samples = page.evaluate("""async data=>{
                            const i=new Image();i.src='data:image/png;base64,'+data;await i.decode();
                            const c=new OffscreenCanvas(i.width,i.height), g=c.getContext('2d');g.drawImage(i,0,0);
                            const px=(x,y)=>Array.from(g.getImageData(x,y,1,1).data).slice(0,3);
                            return Array.from({length:10},(_,k)=>[3,70,136].flatMap(d=>
                              [3,620,694].map(x=>px(x,140*k+d))));
                        }""", base64.b64encode(png).decode())
                        self.assertEqual(samples, [([[k*20,80,120]]*2+[[5,175,50]])*3 for k in range(10)])
                    finally:
                        ctx.close()
