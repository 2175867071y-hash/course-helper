"""截取题目区域并拼接滚动分块；不进行文字识别。"""
import base64
import math
import struct
from browser import ManualRequired


_RECT = r'''(e, inner) => {
    const b=e.getBoundingClientRect();
    let r=inner ? {left:b.left+e.clientLeft+inner.left,top:b.top+e.clientTop+inner.top,
                  right:b.left+e.clientLeft+inner.right,bottom:b.top+e.clientTop+inner.bottom}
                : {left:b.left,top:b.top,right:b.right,bottom:b.bottom};
    if(r.left<0 || r.top<0 || r.right>innerWidth+.5 || r.bottom>innerHeight+.5) return null;
    for(let p=e.parentElement;p;p=p.parentElement){
        if(p===document.body||p===document.documentElement) continue;
        const s=getComputedStyle(p),a=p.getBoundingClientRect();
        const sx=p.offsetWidth?a.width/p.offsetWidth:1,sy=p.offsetHeight?a.height/p.offsetHeight:1;
        if((/hidden|clip|scroll|auto/.test(s.overflowX) &&
             (r.left<a.left+p.clientLeft*sx-.5 || r.right>a.left+(p.clientLeft+p.clientWidth)*sx+.5)) ||
           (/hidden|clip|scroll|auto/.test(s.overflowY) &&
             (r.top<a.top+p.clientTop*sy-.5 || r.bottom>a.top+(p.clientTop+p.clientHeight)*sy+.5))) return null;
    }
    const owner=e.hasAttribute('data-ch-capture')?e.parentElement:e;
    const dx=Math.min(2,(r.right-r.left)/4),dy=Math.min(2,(r.bottom-r.top)/4);
    for(const x of [r.left+dx,(r.left+r.right)/2,r.right-dx])
      for(const y of [r.top+dy,(r.top+r.bottom)/2,r.bottom-dy]){
        const hit=document.elementFromPoint(x,y);
        if(!hit || (hit!==owner && !owner.contains(hit)))return null;
      }
    return r;
}'''


def _visible_rect(marker):
    rect = marker.evaluate(_RECT, None)
    frame = marker.owner_frame()
    while rect and frame.parent_frame:
        element = frame.frame_element()
        try:
            if element.evaluate("e=>getComputedStyle(e).transform!=='none' || (e.currentCSSZoom||1)!==1"):
                raise ManualRequired('课件窗口使用缩放变换，暂不能保证截图完整。')
            rect = element.evaluate(_RECT, rect)
        finally:
            element.dispose()
        frame = frame.parent_frame
    if rect is None:
        raise ManualRequired('题目截图被滚动区域或网页浮层遮挡，请展开答题区域后继续。')
    return rect


def _capture_tile(page, session, rect, w, h):
    layout = session.send('Page.getLayoutMetrics')
    view = layout['cssVisualViewport']
    zoom = view.get('zoom', 1)
    if abs(view['scale'] - 1) > .001:
        raise ManualRequired('答题页正在使用手势放大，请恢复页面手势缩放后重试。')
    if abs(zoom - 1) < .001:
        png = page.screenshot(type='png', animations='disabled', caret='hide', scale='css',
            timeout=5000, clip=dict(x=rect['left'], y=rect['top'], width=w, height=h))
    else:
        # DOM coordinates are in CSS pixels; Chromium clips in device-independent pixels.
        # Translate the viewport to document coordinates before applying native site zoom.
        dpr = page.evaluate('window.devicePixelRatio')
        viewport = layout['cssLayoutViewport']
        clip = dict(x=(viewport['pageX']+rect['left'])*zoom, y=(viewport['pageY']+rect['top'])*zoom,
                    width=w*zoom, height=h*zoom, scale=1/dpr)
        encoded = session.send('Page.captureScreenshot', dict(format='png', clip=clip,
                               fromSurface=True, captureBeyondViewport=True))['data']
        png = base64.b64decode(encoded, validate=True)
    actual_w, actual_h = struct.unpack('>II', png[16:24])
    if abs(actual_w-w)>1 or abs(actual_h-h)>1:
        raise ManualRequired(f'截图分块尺寸不符：预期 {w}×{h}，收到 {actual_w}×{actual_h}，'
                             f'网页缩放 {zoom:.0%}。请在课程窗口按 Ctrl+0 后重试。')
    return png


def capture_region(node, check=lambda: None):
    """按原始尺寸拼接实际可见分块，所有临时定位元素均在 finally 中移除。"""
    check()
    if not node.is_visible():
        raise ManualRequired('题目区域尚未显示，无法截图。')
    if node.evaluate("e=>[e,...e.querySelectorAll('img')].some(n=>n.tagName==='IMG'&&(!n.complete||!n.naturalWidth))"):
        raise ManualRequired('题目图片尚未加载完整，请稍后继续。')
    if node.evaluate("e=>['IMG','CANVAS','svg'].includes(e.tagName)"):
        from quiz_media import image_bytes
        return image_bytes(node)
    if node.evaluate('''e=>getComputedStyle(e).position==='static' &&
      [...e.querySelectorAll('*')].some(n=>getComputedStyle(n).position==='absolute'&&
        n.offsetParent && !e.contains(n.offsetParent))'''):
        raise ManualRequired('题目使用跨区域绝对定位，暂不能保证截图保留原布局。')
    # Use the same frame-local bounds as the tile markers; top-level CDP bounds
    # can accumulate fractional rounding through zoomed iframe offsets.
    # overflow:visible 的内容会画到边框外；按滚动尺寸扩大区域，避免静默漏掉底部或右侧。
    box = node.evaluate('''e=>{
      const s=getComputedStyle(e), b=e.getBoundingClientRect();
      const extraX=/visible/.test(s.overflowX) ? b.width-e.clientWidth : 0;
      const extraY=/visible/.test(s.overflowY) ? b.height-e.clientHeight : 0;
      const width=/visible/.test(s.overflowX) ? Math.max(b.width, e.scrollWidth+extraX) : b.width;
      const height=/visible/.test(s.overflowY) ? Math.max(b.height, e.scrollHeight+extraY) : b.height;
      return {width, height};
    }''')
    if not box:
        raise ManualRequired('题目区域尺寸无法确定。')
    # Blink may report an integer as e.g. 1400.000122 at fractional DPI.
    # Do not invent a one-pixel row outside the question.
    width, height = (math.ceil(box[axis] - .001) for axis in ('width', 'height'))
    if not (4 <= width <= 4096 and 4 <= height <= 8192 and width*height <= 8_000_000):
        raise ManualRequired('单题截图过大，请调整题目显示；不会截断题目。')
    # Every enclosing frame/scroll pane must fit an individual tile.
    limits = node.evaluate('''e=>{let w=innerWidth,h=innerHeight;
      for(let n=e.parentElement;n;n=n.parentElement){
        if(n===document.body||n===document.documentElement)continue;
        const s=getComputedStyle(n);
        if(/hidden|clip|scroll|auto/.test(s.overflowX))w=Math.min(w,n.clientWidth);
        if(/hidden|clip|scroll|auto/.test(s.overflowY))h=Math.min(h,n.clientHeight);
      }return [w,h]}''')
    handle = node.element_handle()
    frame = handle.owner_frame()
    handle.dispose()
    while frame.parent_frame:
        frame = frame.parent_frame
        frame_size = frame.evaluate('[innerWidth,innerHeight]')
        limits = [min(a,b) for a,b in zip(limits, frame_size)]
    tw, th = min(600, int(limits[0])-40), min(400, int(limits[1])-120)
    if min(tw, th) < 40 or math.ceil(width/tw)*math.ceil(height/th) > 160:
        raise ManualRequired('答题区域太小或题目过长，请放大答题区域后继续。')
    state = node.evaluate('''e=>{const old=e.getAttribute('style');
      if(getComputedStyle(e).position==='static')e.style.position='relative';return old}''')
    marker = None
    session = None
    stage = '定位截图区域'
    pieces = []
    try:
        session = node.page.context.new_cdp_session(node.page)
        marker = node.evaluate_handle('''e=>{const m=document.createElement('span');
          m.setAttribute('data-ch-capture','');
          m.style.cssText='position:absolute!important;display:block!important;margin:0!important;'+
            'padding:0!important;border:0!important;pointer-events:none!important;background:transparent!important;';
          e.append(m);return m}''').as_element()
        for y in range(0, height, th):
            for x in range(0, width, tw):
                check()
                w,h = min(tw,width-x),min(th,height-y)
                aligned = marker.evaluate('''(e,p)=>{const n=e.parentElement;
                  Object.assign(e.style,{left:'0',top:'0',width:'100px',height:'100px'});
                  const a=n.getBoundingClientRect(),b=e.getBoundingClientRect();
                  const sx=b.width/100,sy=b.height/100;
                  if(!sx||!sy)return false;
                  Object.assign(e.style,{left:(a.left+p.x-b.left)/sx+'px',top:(a.top+p.y-b.top)/sy+'px',
                    width:p.w/sx+'px',height:p.h/sy+'px'});
                  e.scrollIntoView({block:'center',inline:'center',behavior:'instant'});
                  const root=n.getBoundingClientRect(),tile=e.getBoundingClientRect();
                  return Math.abs(tile.left-root.left-p.x)<.5 && Math.abs(tile.top-root.top-p.y)<.5 &&
                    Math.abs(tile.width-p.w)<.5 && Math.abs(tile.height-p.h)<.5;
                }''', dict(x=x,y=y,w=w,h=h))
                if not aligned:
                    raise ManualRequired('无法对齐完整题图的滚动分块，请检查页面缩放或布局。')
                stage = f'截取分块（第 {y//th+1} 行、第 {x//tw+1} 列）'
                for attempt in range(3):
                    if attempt:
                        node.page.wait_for_timeout(120 * attempt)
                        check()
                    rect = _visible_rect(marker)
                    png = _capture_tile(node.page, session, rect, w, h)
                    after = _visible_rect(marker)
                    if all(abs(after[k]-rect[k]) <= .5 for k in rect):
                        break
                else:
                    raise ManualRequired('截图时页面仍在移动，请稍后继续。')
                pieces.append(dict(x=x,y=y,w=w,h=h,data=base64.b64encode(png).decode('ascii')))
        stage = '拼接完整题图'
        encoded = node.page.evaluate('''async ({width,height,pieces})=>{
          const c=document.createElement('canvas');c.width=width;c.height=height;
          const g=c.getContext('2d');
          for(const p of pieces){const i=new Image();i.src='data:image/png;base64,'+p.data;
            await i.decode();
            if(Math.abs(i.width-p.w)>1||Math.abs(i.height-p.h)>1)throw Error('incomplete tile');
            g.drawImage(i,p.x,p.y,p.w,p.h);}
          return c.toDataURL('image/png').split(',')[1];}''', dict(width=width,height=height,pieces=pieces))
        result = base64.b64decode(encoded, validate=True)
        if len(result)>4*1024*1024:
            raise ManualRequired('单题截图超过 4 MiB，请调整显示后继续。')
        return result
    except ManualRequired:
        raise
    except Exception as exc:
        raise ManualRequired(f'{stage}失败（{type(exc).__name__}）。本批未发送，请重试。') from exc
    finally:
        if session:
            try:
                session.detach()
            except Exception:
                pass
        if marker:
            try:
                marker.evaluate('e=>e.remove()')
                marker.dispose()
            except Exception:
                pass
        try:
            node.evaluate('(e,s)=>s===null?e.removeAttribute("style"):e.setAttribute("style",s)', state)
        except Exception:
            pass
