"""截图时展开题内滚动区和下拉选项，随后恢复原样。"""
from contextlib import contextmanager
from browser import ManualRequired


@contextmanager
def expanded_region(root, description='题目或材料'):
    state = root.evaluate_handle('''e=>{
      const nodes=[e,...e.querySelectorAll('*')];
      return nodes.map(n=>({node:n,style:n.getAttribute('style'),size:n.getAttribute('size'),
        top:n.scrollTop,left:n.scrollLeft}));
    }''')
    try:
        root.evaluate('''e=>{
          for(const n of e.querySelectorAll('select')){
            n.size=Math.max(2,n.options.length);
            n.style.setProperty('height','auto','important');n.style.setProperty('max-height','none','important');
            // Native listboxes keep their CSS width even after all options become visible.
            if(n.scrollWidth>n.clientWidth+2 && n.scrollWidth<4096){
              const width=(n.scrollWidth+n.offsetWidth-n.clientWidth)+'px';
              n.style.setProperty('width',width,'important');
              n.style.setProperty('min-width',width,'important');
              n.style.setProperty('max-width','none','important');
            }
          }
          // Inside-out expansion also grows an explicitly sized enclosing question.
          for(const n of [e,...e.querySelectorAll('*')].reverse()){
            if(!n.getClientRects().length||getComputedStyle(n).visibility==='hidden')continue;
            if(n.matches('input,textarea,select,img,svg,canvas,video,audio'))continue;
            const s=getComputedStyle(n);
            if(s.webkitLineClamp && s.webkitLineClamp!=='none'){
              n.style.setProperty('-webkit-line-clamp','unset','important');
              n.style.setProperty('max-height','none','important');
            }
            if(n.scrollHeight>n.clientHeight+2 && n.scrollHeight<8192){
              const height=(n.scrollHeight+n.offsetHeight-n.clientHeight)+'px';
              n.style.setProperty('height',height,'important');
              // A flex parent can shrink an explicit size; retain the expanded minimum too.
              n.style.setProperty('min-height',height,'important');
              n.style.setProperty('max-height','none','important');
              n.style.setProperty('overflow-y','visible','important');n.scrollTop=0;
            }
            if(n.scrollWidth>n.clientWidth+2 && n.scrollWidth<4096){
              // A percentage-width list plus padding/border can stay wider than its parent
              // however far we expand the parent. Measure its original used CSS width first.
              const measured=[...n.children].filter(c=>c instanceof HTMLElement &&
                c.offsetWidth && !c.matches('input,textarea,select,img,canvas,video,audio'))
                .map(c=>({c,css:getComputedStyle(c).width,outer:c.offsetWidth}));
              const width=(n.scrollWidth+n.offsetWidth-n.clientWidth)+'px';
              n.style.setProperty('width',width,'important');
              n.style.setProperty('min-width',width,'important');
              n.style.setProperty('max-width','none','important');
              n.style.setProperty('overflow-x','visible','important');n.scrollLeft=0;
              if(n.scrollWidth>n.clientWidth+2){
                const box=n.getBoundingClientRect();
                const right=box.left+(n.clientLeft+n.clientWidth)*(box.width/n.offsetWidth);
                for(const {c,css,outer} of measured){
                  if(!css.endsWith('px') || c.offsetWidth<=outer ||
                     c.getBoundingClientRect().right<=right)continue;
                  // Preserve the original layout (including padding/border) in the enlarged
                  // capture box. Leave unrelated children and truly oversized content alone.
                  for(const prop of ['width','min-width','max-width'])
                    c.style.setProperty(prop,css,'important');
                }
              }
            }
          }
        }''')
        clipped = root.evaluate('''e=>{
          for(const n of [e,...e.querySelectorAll('*')]){
            const s=getComputedStyle(n);
            if(!n.getClientRects().length||s.visibility==='hidden'||
              n.matches('input,textarea,img,svg,canvas,video,audio,option'))continue;
            // overflow:visible 的内容仍会画在边框外，由 capture_region 扩大截图区域，
            // 这里只报告真正被裁剪掉的隐藏/滚动内容。
            for(const [axis,overflow,client,scroll] of [
              ['横向',s.overflowX,n.clientWidth,n.scrollWidth],['纵向',s.overflowY,n.clientHeight,n.scrollHeight]]){
              if(!/hidden|clip|scroll|auto/.test(overflow))continue;
              if(scroll>client+2) return [axis,client,scroll];
            }
          }
        }''')
        if clipped:
            axis, visible, total = clipped
            raise ManualRequired(f'{description}仍有{axis}裁剪：可见 {visible} 像素，内容 {total} 像素。未发送截图。')
        yield
    finally:
        try:
            state.evaluate('''rows=>{for(const s of rows){
              if(!s.node.isConnected)continue;
              s.style===null?s.node.removeAttribute('style'):s.node.setAttribute('style',s.style);
              s.size===null?s.node.removeAttribute('size'):s.node.setAttribute('size',s.size);
              s.node.scrollTop=s.top;s.node.scrollLeft=s.left;
            }}''')
        finally:
            state.dispose()
