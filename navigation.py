"""基于可见 DOM 的目录识别、两点定位与切页确认；不调用网站内部接口。"""
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlsplit


# 同一结构可用于不同站点；没有语义目录时，由用户在网页中定位。
DOM_HELPERS = r"""
const visible = e => !!e && e.getClientRects().length > 0 &&
  getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none';
const marked = n => (n.hasAttribute('aria-current') &&
    !['false',''].includes(n.getAttribute('aria-current'))) ||
    n.getAttribute('aria-selected') === 'true' ||
    [...n.classList].some(c => /(^|[-_])(active|current|selected|cur|on)($|[-_])/i.test(c));
const active = e => {
  return marked(e) || [...e.querySelectorAll('[aria-current],[aria-selected],[class]')].some(marked);
};
const stateClass = c => /(^|[-_])(active|current|selected|cur|on|done|complete|completed|disabled|locked)($|[-_])/i.test(c);
const selectorFor = e => {
  const classes=[...e.classList].filter(c=>!stateClass(c));
  if (!classes.length && !['A','BUTTON','LI'].includes(e.tagName)) return null;
  return e.tagName.toLowerCase()+classes.map(c=>'.'+CSS.escape(c)).join('');
};
const path = e => {
  if (e.id) return '#' + CSS.escape(e.id);
  const parts=[];
  while (e && e.nodeType === 1) {
    if (e.id) {parts.unshift('#'+CSS.escape(e.id)); break;}
    const tag=e.tagName.toLowerCase();
    const siblings=e.parentElement ? [...e.parentElement.children].filter(n=>n.tagName===e.tagName) : [e];
    parts.unshift(tag+':nth-of-type('+(siblings.indexOf(e)+1)+')'); e=e.parentElement;
  }
  return parts.join(' > ');
};
const rowsFor = (root,selector) => [...root.querySelectorAll(selector)].filter(visible)
  .filter(e=> ![...e.querySelectorAll(selector)].some(visible));
const describe = (root,selector,selection=null) => {
  const rows=rowsFor(root,selector);
  const selected=rows.map((e,i)=>(selection ?
    getComputedStyle(e).getPropertyValue(selection.property)===selection.value : active(e))?i:-1).filter(i=>i>=0);
  if (!rows.length || selected.length!==1) return null;
  return {root:path(root), selector, current:selected[0],
    items:rows.map(e=>({path:path(e), text:(e.innerText||e.getAttribute('aria-label')||'').trim().slice(0,160),
      href:e.closest('a')?.href || e.querySelector('a')?.href || '',
      disabled:e.matches(':disabled') || e.getAttribute('aria-disabled')==='true' ||
        /(^|[\s_-])(disabled|locked)([\s_-]|$)/i.test(e.className || '')}))};
};
const catalogLabel = e => /^(目录|课程目录|章节目录|contents|table of contents|course outline|syllabus)$/i.test((e.innerText||'').trim());
const labelsIn = root => [...root.querySelectorAll('div,span,h2,h3,h4,button,[role=tab]')]
  .filter(e=>visible(e) && catalogLabel(e) && ![...e.children].some(catalogLabel));
const catalogBoundary = e => e.matches('aside,nav,[role=navigation],[role=tree]') ||
  /(^|[-_\s])(sidebar|side-bar|catalog|catalogue|directory|outline|toc|syllabus|menu|tree|contents)($|[-_\s])/i.test(e.id+' '+e.className);
const broaderRoot = (root,left,right) => {
  const sidebar=root.closest('aside,nav,[role=navigation],[role=tree]');
  if (sidebar && sidebar.contains(right)) return sidebar;
  // 普通 div 侧栏也保留所有已展开分组，但不扩大到含课件正文的页面容器。
  for (let candidate=root; candidate && candidate!==document.body; candidate=candidate.parentElement) {
    if (candidate.matches('main,article,[role=main]') || candidate.querySelector('main,article,[role=main],video,audio,iframe,textarea')) break;
    if (catalogBoundary(candidate) && labelsIn(candidate).some(label=>!left.contains(label) && !right.contains(label))) return candidate;
  }
  return root;
};
const customCatalogs = () => {
  const found=[];
  for (const label of labelsIn(document.body)) {
    for (let root=label.parentElement; root && root!==document.body; root=root.parentElement) {
      if (root.matches('main,article,[role=main]') || root.querySelector('main,article,[role=main],video,audio,iframe,textarea')) break;
      if (!catalogBoundary(root)) continue;
      const candidates=[];
      for (const current of [...root.querySelectorAll('[class],[aria-current],[aria-selected]')].filter(e=>visible(e)&&marked(e))) {
        let best=null;
        for (let row=current; row && row!==root; row=row.parentElement) {
          const selector=selectorFor(row);
          if (!selector) continue;
          const rows=rowsFor(root,selector);
          if (rows.length<2 || rows.some(e=>e.contains(label) || !(e.innerText||'').trim() ||
              (e.innerText||'').trim().length>160 || (!e.matches('a,button,[role=treeitem],[role=link],[role=button],[role=tab]') &&
              getComputedStyle(e).cursor!=='pointer'))) continue;
          const info=describe(root,selector);
          if (info && (row===rows[info.current] || row.contains(rows[info.current]))) {best=info; break;}
        }
        if (best && !candidates.some(c=>c.items[c.current].path===best.items[best.current].path)) candidates.push(best);
      }
      if (candidates.length) {
        for (const candidate of candidates) {
          if (!found.some(c=>c.root===candidate.root && c.items[c.current].path===candidate.items[candidate.current].path)) found.push(candidate);
        }
      }
      // 当前目录尚无高亮时，不向页面容器继续寻找正文里的选中按钮。
      break;
    }
  }
  return found;
};
"""

CATALOG_JS = 'rule => {' + DOM_HELPERS + r"""
if (rule) {
  const roots=[...document.querySelectorAll(rule.root)].filter(visible);
  return roots.map(root=>describe(root,rule.items,rule.selection)).filter(Boolean);
}
const roots=[...document.querySelectorAll('nav,[role=navigation],[role=tree]')].filter(visible)
  .filter(e=> /课程|章节|目录|course|chapter|syllabus|contents|lesson/i.test(e.getAttribute('aria-label')||'') ||
    e.getAttribute('role')==='tree');
const semantic=roots.filter(e=> !roots.some(n=>n!==e && e.contains(n)))
  .map(root=>describe(root,'a,button,[role=treeitem],[role=tab],[role=link],[role=button]')).filter(Boolean);
return semantic.length ? semantic : customCatalogs();
}"""

CONTENT_JS = r"""exclude => {
  const visible=e=>e.getClientRects().length>0 && getComputedStyle(e).visibility!=='hidden';
  const candidates=[...document.querySelectorAll('main,[role=main],article')].filter(visible);
  const roots=candidates.filter(e=>!candidates.some(n=>n!==e && n.contains(e)));
  const selected=roots.length ? roots : [document.body];
  const excluded=exclude ? [...document.querySelectorAll(exclude)] : [];
  return {busy:!!document.querySelector('[aria-busy=true]'), content:selected.filter(e=>
    !excluded.some(n=>n===e || n.contains(e))).map(e=>{
    const copy=e.cloneNode(true);
    const removals=[...e.querySelectorAll('nav,aside,header,footer,script,style,input,textarea,select,[role=navigation],[role=tree],[role=dialog],#course-helper-locate'),
      ...excluded.filter(n=>e.contains(n))];
    // 目录可能只有从 html 开始的绝对 CSS 路径，必须先在原 DOM 中定位，不能拿它查克隆的 body。
    const copies=[copy,...copy.querySelectorAll('*')];
    const positions=new Map([e,...e.querySelectorAll('*')].map((n,i)=>[n,i]));
    for (const node of removals) copies[positions.get(node)]?.remove();
    return {text:(copy.textContent||'').replace(/\s+/g,' ').trim().slice(0,48000),
      media:[...e.querySelectorAll('video,audio,img,iframe,embed,object')].filter(visible)
        .filter(n=>!removals.some(removed=>removed===n || removed.contains(n)))
        .map(n=>[n.tagName,n.currentSrc||n.src||n.data||'',n.getAttribute('title')||''])};
  })};
}"""

PICK_JS = 'action => {' + DOM_HELPERS + r"""
const key='__courseHelperDirectoryPicker';
const events=['pointerdown','pointerup','mousedown','mouseup','click','dblclick'];
const cleanup=state=>{
  for (const name of events) window.removeEventListener(name,state.listener,true);
  state.banner.remove();
};
if (action !== 'start') {
  const previous=window[key];
  if (!previous) return action==='finish' ? {stage:'lost'} : null;
  cleanup(previous); delete window[key];
  return action==='finish' ? {stage:previous.result ? 'picked' : previous.first ? 'first' : 'empty',
    result:previous.result} : null;
}
if (window[key]) {
  cleanup(window[key]);
}
const banner=document.createElement('div'); banner.id='course-helper-locate';
banner.textContent='目录定位（0 / 2）：先点击目录里当前高亮的项目，再点击紧接着的下一项。点击只用于定位。';
banner.style.cssText='position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#183448;color:white;padding:12px;font:16px sans-serif;pointer-events:none;';
document.body.append(banner);
const state={banner,first:null,result:null};
state.listener=event=>{
  if (!event.isTrusted) return;
  event.preventDefault(); event.stopImmediatePropagation();
  if (event.type!=='click' || event.button!==0) return;
  const target=event.composedPath().find(e=>e instanceof Element);
  if (!(target instanceof Element) || target===banner) return;
  if (!state.first) {state.first=target; banner.textContent='目录定位（1 / 2）：已记录当前项，请点击目录中紧接着的下一项。'; return;}
  const a=state.first,b=target;
  if (!a.isConnected) {state.result={error:'目录在定位期间重新加载，请重新定位。'}; banner.textContent=state.result.error; return;}
  if (a===b) {banner.textContent='目录定位（1 / 2）：两次选择相同，请点击下一项。'; return;}
  const ancestors=e=>{const out=[];while(e && e!==document.body){out.push(e);e=e.parentElement;}return out;};
  let found=null;
  for (const left of ancestors(a)) {
    for (const right of ancestors(b)) {
      if (left===right || left.contains(right) || right.contains(left) || left.tagName!==right.tagName) continue;
      const common=[...left.classList].filter(c=>right.classList.contains(c) &&
        !stateClass(c));
      if (!common.length && !['A','BUTTON','LI'].includes(left.tagName)) continue;
      const selector=left.tagName.toLowerCase()+common.map(c=>'.'+CSS.escape(c)).join('');
      let root=left.parentElement;
      while(root && !root.contains(right)) root=root.parentElement;
      if (!root || root===document.body || root===document.documentElement) continue;
      // 侧栏内同一组的前两项不能把范围限制在该组，保留后续已展开的分组。
      root=broaderRoot(root,left,right);
      const rows=rowsFor(root,selector), first=rows.indexOf(left), second=rows.indexOf(right);
      if (first<0 || second!==first+1 || rows.length<2) continue;
      let info=describe(root,selector), selection=null;
      // 用户明确指出当前项后，允许学习其唯一底色/文字颜色；不凭颜色猜未指认的当前项。
      if (!info && !rows.some(active)) {
        for (const property of ['background-color','color']) {
          const value=getComputedStyle(left).getPropertyValue(property);
          if (rows.filter(e=>getComputedStyle(e).getPropertyValue(property)===value).length!==1) continue;
          selection={property,value}; info=describe(root,selector,selection); break;
        }
      }
      if (!info || info.current!==first) continue;
      found={root:path(root),items:selector};
      if (selection) found.selection=selection;
      break;
    }
    if(found)break;
  }
  state.result=found || {error:'无法确认共同目录和当前高亮项。请重新定位，点击项目整行；两项必须相邻。'};
  banner.textContent=found ? '目录定位（2 / 2）已完成。请回到助手点击“完成定位”。' : state.result.error;
};
window[key]=state;
for (const name of events) window.addEventListener(name,state.listener,{capture:true,passive:false});
return true;
}"""


def scope(url):
    parts = urlsplit(url)
    return parts.scheme + '://' + parts.netloc + parts.path


def origin(url):
    parts = urlsplit(url)
    return parts.scheme + '://' + parts.netloc


class NavigationTarget:
    def __init__(self, navigator, locator, target=None):
        self.navigator, self.locator, self.target = navigator, locator, target

    def __getattr__(self, name):
        return getattr(self.locator, name)

    def click(self, **kwargs):
        self.navigator.begin_transition(self.target)
        self.locator.click(**kwargs)


class Navigator:
    def __init__(self, adapter):
        self.adapter = adapter
        self.rule = None
        self.rule_path = None
        self.pending = None
        self.picker_frames = []

    def fail(self, message):
        from browser import ManualRequired
        raise ManualRequired(message)

    def load(self, path):
        self.rule_path = Path(path)
        try:
            rule = json.loads(self.rule_path.read_text(encoding='utf-8'))
            if isinstance(rule, dict) and all(isinstance(rule.get(k), str) and
                    0 < len(rule[k]) < 4096 for k in ('page', 'frame', 'root', 'items')):
                selection = rule.get('selection')
                if selection is not None and (not isinstance(selection, dict) or
                        selection.get('property') not in ('background-color', 'color') or
                        not isinstance(selection.get('value'), str) or
                        not 0 < len(selection['value']) < 128):
                    return
                self.rule = rule
        except (OSError, ValueError):
            pass

    def state(self):
        if not callable(getattr(self.adapter.page, 'evaluate', None)):
            return None
        found = []
        for frame in self.adapter.visible_frames():
            rule = None
            if self.rule:
                if origin(self.rule['page']) != origin(self.adapter.page.url) or origin(self.rule['frame']) != origin(frame.url):
                    continue
                rule = self.rule
            for item in frame.evaluate(CATALOG_JS, rule):
                item['frame'] = frame
                found.append(item)
        if len(found) > 1:
            self.fail('找到多个活动目录，无法唯一确定下一项。请使用“定位目录”指定本课程目录。')
        return found[0] if found else None

    def identity(self):
        item = self.state()
        if not item:
            return ''
        row = item['items'][item['current']]
        # 标题可能重复；目录范围及顺序位置必须同时参与身份判断。
        material = [scope(item['frame'].url), item['root'], item['current'], row['text']]
        return hashlib.sha256(json.dumps(material).encode()).hexdigest()[:24]

    def next_target(self):
        item = self.state()
        if not item:
            return None
        index = item['current'] + 1
        if index >= len(item['items']):
            self.fail('已到当前展开目录的末项。请核对是否还有折叠单元或未完成内容，再选择结束本课程。')
        target = item['items'][index]
        if target['disabled']:
            self.fail('目录中的下一项已锁定或不可用，请先检查本节的完成要求。')
        self.adapter.check_navigation_url(target['href'])
        locator = item['frame'].locator(target['path'])
        return NavigationTarget(self, locator,
                                (origin(item['frame'].url), item['root'], index, target['text']))

    def fingerprint(self):
        # 不输出正文或媒体 URL；只用于本进程确认切换。
        state = self.state()
        values = []
        for frame in self.adapter.visible_frames():
            data = frame.evaluate(CONTENT_JS, state['root'] if state and state['frame'] == frame else '')
            if data['busy']:
                return None
            values.append(data['content'])
        return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()

    def begin_transition(self, target):
        if self.pending:
            self.fail('上次切换尚未确认正文更新，请等待页面加载或手动切换后继续；不会重复点击。')
        before = self.fingerprint()
        if before is None:
            self.fail('当前页面仍在加载，暂不能切换。')
        self.pending = {'before': before, 'target': target, 'seen': None, 'since': 0}

    def ready(self):
        if not self.pending:
            return True
        target = self.pending['target']
        state = self.state()
        if target and (not state or (origin(state['frame'].url), state['root'], state['current'],
                                    state['items'][state['current']]['text']) != target):
            return False
        current = self.fingerprint()
        if current is None or current == self.pending['before']:
            self.pending['seen'] = None
            return False
        if current != self.pending['seen']:
            self.pending.update(seen=current, since=time.monotonic())
            return False
        if time.monotonic() - self.pending['since'] < 0.35:
            return False
        self.pending = None
        return True

    def wait_ready(self, timeout):
        if not self.pending:
            return
        deadline = time.monotonic() + timeout
        while self.pending:
            if self.adapter.control:
                self.adapter.control.check()
            reason = self.adapter.guard(include_course=False)
            if reason:
                self.fail(reason)
            if self.ready():
                return
            if time.monotonic() >= deadline:
                self.fail('已点击下一项，但尚未确认正文或播放器更新。请检查加载状态后继续；不会重复点击。')
            if self.adapter.control:
                self.adapter.control.sleep(0.2)
            else:
                time.sleep(0.2)

    def ensure_ready(self):
        if self.pending:
            self.wait_ready(1)

    def acknowledge(self):
        # 用户可能手动打开了另一项；仍必须确认正文更新，不能只清空防重复状态。
        if self.pending:
            self.pending.update(target=None, seen=None)

    def start_picker(self):
        self.cancel_picker()
        try:
            for frame in self.adapter.visible_frames():
                frame.evaluate(PICK_JS, 'start')
                self.picker_frames.append(frame)
        except Exception:
            self.cancel_picker()
            raise

    def cancel_picker(self):
        for frame in self.picker_frames:
            try:
                frame.evaluate(PICK_JS, 'cancel')
            except Exception:
                pass
        self.picker_frames = []

    def finish_picker(self):
        picked = []
        reports = []
        try:
            for frame in self.picker_frames:
                try:
                    report = frame.evaluate(PICK_JS, 'finish')
                except Exception:
                    report = {'stage': 'lost'}
                reports.append(report)
                result = report.get('result') if report else None
                if result and 'error' not in result:
                    picked.append(dict(result, page=scope(self.adapter.page.url), frame=scope(frame.url)))
        finally:
            self.cancel_picker()
        touched = [r for r in reports if r and r.get('stage') in ('first', 'picked')]
        if len(touched) > 1:
            self.fail('点击记录来自不同的页面区域，请在同一份目录里依次点击当前项和下一项。')
        if not picked:
            if touched:
                result = touched[0].get('result') or {}
                self.fail(result.get('error') or
                          '只记录了当前项（1 / 2）。请重新定位，再点击同一目录中紧接着的下一项；不要重复点当前项。')
            if reports and all(not r or r.get('stage') == 'lost' for r in reports):
                self.fail('定位期间网页已刷新或切换，点击记录已失效。请等目录加载好后重新定位。')
            self.fail('尚未记录目录点击（0 / 2）。请在助手为本任务打开的浏览器中，看到顶部“目录定位”提示后，依次点击当前项和下一项。')
        if len(picked) != 1:
            self.fail('记录了多份目录，请重新定位并只选择本课程的目录。')
        previous = self.rule
        self.rule = picked[0]
        try:
            if not self.state():
                self.fail('目录已变化，无法确认当前项，请重新定位。')
            if self.rule_path:
                self.rule_path.write_text(json.dumps(self.rule, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            self.rule = previous
            raise
        return self.rule

    def configure(self, control):
        self.adapter.pause()
        while True:
            try:
                self.start_picker()
                answer = control.ask('在网页中定位目录',
                    '展开课程目录，依次点击当前正在学习的项目和紧接着的下一项。'
                    '定位期间这两次点击不会切换页面。然后回这里点击“完成定位”。',
                    [('done', '完成定位'), ('cancel', '取消定位')])
                if answer == 'cancel':
                    return False
                self.finish_picker()
                control.emit('log', message='目录定位已保存；将按当前高亮项的下一项切换。')
                return True
            except (ValueError, RuntimeError, OSError) as exc:
                from browser import ManualRequired
                message = str(exc) if isinstance(exc, ManualRequired) else '目录定位或保存失败，请检查页面后重试。'
                if control.ask('定位未完成', message,
                        [('retry', '重新定位'), ('cancel', '取消定位')]) == 'cancel':
                    return False
            finally:
                self.cancel_picker()
