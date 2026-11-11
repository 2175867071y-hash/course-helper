"""以题目截图提供内容；DOM 只负责控件绑定和本地变化检测。"""
import base64
from types import SimpleNamespace
from contextlib import contextmanager

from browser import ManualRequired
from quiz_ai import QuizImage, QuizAudio, QuizQuestion, QuizBlank
from quiz_capture import capture_region
from quiz_dom import (QUESTION_GROUPS, ANSWER_SELECTOR, OPTION_SELECTOR, TEXT_SELECTOR,
    BANK_SELECTOR, PASSAGE_SELECTOR, INSTRUCTION_SELECTOR, QUESTION_PAGE,
    visible_options, quiz_scope, FillBinding, BlankBinding, _blank)
from quiz_layout import expanded_region
from quiz_drag import is_drag_group, read_drag, SOURCES, TARGETS
from quiz_media import audio_bytes, EXCLUDED


PRIVATE_REGIONS = ('[data-account], [data-profile], [class*="account" i], [id*="account" i], '
    '[class*="profile" i], [id*="profile" i], .user-info, .userInfo, .userinfo, .user-card, .avatar')
# 按钮自身不是材料，但材料容器里常带“展开/播放”按钮；不能因为容器里有按钮就整块跳过。
MATERIAL_EXCLUDED = ('aside,nav,header,footer,[role=search],button,[role=button],script,style, '
    + PRIVATE_REGIONS)
MATERIAL_NESTED = ('aside,nav,header,footer,[role=search],script,style, ' + PRIVATE_REGIONS)
MATERIAL_SELECTORS = ', '.join((BANK_SELECTOR, PASSAGE_SELECTOR, INSTRUCTION_SELECTOR))


def discover_groups(frame):
    """按容器和控件识别，不读取题干或选项文字。"""
    candidates = frame.locator(QUESTION_GROUPS + ', tr, p, li')
    indexes = candidates.evaluate_all('''(nodes,s)=>{
      const visible=e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
      const containers=nodes.filter(e=>e.matches(s.groups)&&visible(e));
      const leaves=containers.filter(e=>!containers.some(n=>n!==e&&e.contains(n)));
      // An inner fieldset can contain just the answers. Keep its stem in the
      // same image, without merging adjacent questions under a section wrapper.
      const groups=leaves.map(leaf=>containers.find(e=>e.contains(leaf)&&
        !leaves.some(other=>other!==leaf&&e.contains(other))&&
        (e===leaf||![...e.querySelectorAll(s.answers)].some(n=>visible(n)&&!leaf.contains(n)))));
      const extra=nodes.filter(e=>visible(e)&&!e.closest('aside,nav,header,footer,[role=search]')&&
        !!e.closest('main,[role=main],form,[role=dialog],.fill-blank-reply')&&
        !!e.querySelector(s.answers)&&!groups.some(g=>g.contains(e)||e.contains(g)));
      return nodes.flatMap((e,i)=>groups.includes(e)||extra.includes(e)&&
        !extra.some(p=>p!==e&&p.contains(e))?[i]:[]);
    }''', dict(groups=QUESTION_GROUPS, answers=ANSWER_SELECTOR+', '+OPTION_SELECTOR))
    return [candidates.nth(i) for i in indexes]


def material_regions(item):
    scope = quiz_scope(item)
    form = item.root[0].locator('xpath=ancestor::form[1]')
    common_form = form.count() == 1 and all(form.evaluate(
        '(e,n)=>e.contains(n)', g.element_handle()) for g in item.root)
    if common_form:
        scope = form
    candidates = scope.locator('*')
    handles = [g.element_handle() for g in item.root]
    try:
        indexes = candidates.evaluate_all('''(nodes,{groups,excluded,nested,materials,commonForm})=>{
          // A mixed parent cannot be discarded if it owns material text directly.
          // Returning no regions would silently omit that text from the model's images.
          if(nodes.some(e=>!e.closest(excluded)&&(commonForm||!!e.closest(materials))&&
            !groups.some(g=>g.contains(e)||e.contains(g))&&e.querySelector(nested)&&
            [...e.childNodes].some(n=>n.nodeType===3&&n.textContent.trim())))return null;
          const selected=nodes.filter(e=>e.getClientRects().length&&
            getComputedStyle(e).visibility!=='hidden'&&
            !e.closest(excluded)&&(!e.querySelector(nested)||e.matches(materials))&&
            (commonForm||!!e.closest(materials))&&
            !groups.some(g=>g.contains(e)||e.contains(g))&&
            !e.matches('input,textarea,select,option,output,audio')&&
            (!e.querySelector('button,input,textarea,select,[role=button]')||e.matches(materials))&&
            (!!e.textContent.trim()||e.matches('img,canvas,svg')||!!e.querySelector('img,canvas,svg')));
          return nodes.flatMap((e,i)=>selected.includes(e)&&
            !selected.some(p=>p!==e&&p.contains(e))?[i]:[]);
        }''', dict(groups=handles, excluded=MATERIAL_EXCLUDED, nested=MATERIAL_NESTED,
                   materials=MATERIAL_SELECTORS, commonForm=common_form))
    finally:
        for handle in handles:
            handle.dispose()
    if indexes is None:
        raise ManualRequired('材料正文混有账户栏或导航控件，无法完整截取材料，请调整页面后继续。')
    return [candidates.nth(i) for i in indexes]


def fingerprint(item):
    """页面内部散列，不把题干、选项或 HTML 交给 Python/AI；忽略作答状态。"""
    values = []
    for node in item.root + material_regions(item):
        values.append(node.evaluate('''e=>{
          const c=e.cloneNode(true);
          c.querySelectorAll('script,style,input[type=hidden],[data-ch-capture]').forEach(n=>n.remove());
          // 选中时常插入空的勾选图标节点；这类作答状态不改变题目内容。
          c.querySelectorAll('span,i,em,b,s,svg,div').forEach(n=>{
            if(n.children.length||(n.textContent||'').trim()||n.querySelector('img,canvas'))return;
            if([...n.classList].some(x=>/check|select|chosen|active|mark|tick|correct/i.test(x)))n.remove();
          });
          const sources=[...c.querySelectorAll('[draggable=true],[data-drag-id],.drag-item')];
          sources.forEach(n=>n.remove());
          const clean=n=>{
            for(const a of ['checked','selected','aria-checked','aria-selected','aria-pressed','value','data-state'])n.removeAttribute(a);
            // 超星等站点把“已选择/选择”写进 aria-label；作答状态变化不应算题目内容变化。
            const label=n.getAttribute('aria-label');
            if(label){
              const rest=label.replace(/(已选择|已选中|已勾选|未选择|请选择|选择|已选)/g,'').replace(/\\s+/g,' ').trim();
              rest?n.setAttribute('aria-label',rest):n.removeAttribute('aria-label');
            }
            n.classList.remove('check_answer','check_answer_dx','active','selected','checked','chosen','is-checked');
            if(!n.classList.length)n.removeAttribute('class');
            if(!n.getAttribute('style'))n.removeAttribute('style');
            if(n.tagName==='TEXTAREA')n.replaceChildren();
          };
          [c,...c.querySelectorAll('*'),...sources,...sources.flatMap(n=>[...n.querySelectorAll('*')])].forEach(clean);
          const pictures=[e,...e.querySelectorAll('canvas')].filter(n=>n.tagName==='CANVAS')
            .map(n=>{try{return n.toDataURL()}catch(_){return 'unverifiable-canvas'}}).join('');
          // 有些题库用样式表显示题号、选项或补充题干。只散列可见文字，跳过勾选图标。
          const generated=[];
          for(const n of [e,...e.querySelectorAll('*')]){
            if(!n.getClientRects().length || n.closest('[data-ch-capture]'))continue;
            for(const side of ['::before','::after']){
              const style=getComputedStyle(n,side);
              if(style.visibility==='hidden'||style.display==='none')continue;
              const raw=style.content;
              if(!raw.startsWith('"')&&!raw.startsWith("'"))continue;
              const content=raw.slice(1,-1).trim();
              if(/[\\p{L}\\p{N}]/u.test(content)&&!['已选择','已选中','已勾选','未选择','选择','已选'].includes(content))
                generated.push([side,content]);
            }
          }
          const s=c.outerHTML+sources.map(n=>n.outerHTML).sort().join('')+pictures+JSON.stringify(generated);
          return [2166136261,2246822519,3266489917,668265263].map(seed=>{
            let h=seed;for(let i=0;i<s.length;i++)h=Math.imul(h^s.charCodeAt(i),16777619);
            return (h>>>0).toString(16);
          }).join(':')+':'+s.length;
        }'''))
    return '|'.join(values)


def attempt_scope(item):
    # Content revisions do not create another submission attempt within one Task.
    identifiers = [g.evaluate('''e=>[e.id,e.getAttribute('data-question-id'),
      ...[...e.querySelectorAll('input,select,textarea,[role=radio],[role=checkbox]')]
        .map(n=>n.id||n.getAttribute('name')||'')]''') for g in item.root]
    return item.context + '|' + str(item.task_context) + '|' + str(identifiers)


def check_structure(item):
    scope = quiz_scope(item)
    for material in scope.locator(', '.join((BANK_SELECTOR, PASSAGE_SELECTOR, INSTRUCTION_SELECTOR))).all():
        if not material.is_visible():
            raise ManualRequired('题目材料仍隐藏或折叠，请展开完整材料后再截图。')
    for region in item.root + material_regions(item):
        if region.evaluate('(e,s)=>!!e.closest(s)||!!e.querySelector(s)', PRIVATE_REGIONS):
            raise ManualRequired('题目区域混有账户信息，无法仅截取题目，请调整页面后继续。')
        if region.evaluate('''e=>{
          if(e.matches('details:not([open])')||e.querySelector('details:not([open])'))return true;
          return [e,...e.querySelectorAll('*')].some(n=>{
            if(n.closest('script,style,template,input,select,textarea,audio'))return false;
            const content=n.matches('img,canvas,svg,video,object,embed')||
              [...n.childNodes].some(c=>c.nodeType===3&&c.textContent.trim());
            return content&&(!n.getClientRects().length||getComputedStyle(n).visibility==='hidden'||
              getComputedStyle(n).display==='none');
          });
        }'''):
            raise ManualRequired('题目或材料内部还有隐藏或折叠内容，请展开完整内容后再截图。')
    for group in scope.locator(QUESTION_GROUPS).all():
        if not group.is_visible() and group.locator(ANSWER_SELECTOR+', '+OPTION_SELECTOR).count():
            raise ManualRequired('小测还有隐藏或折叠题目，请展开后再截图。')
    known = [n for g in item.root for n in g.locator(TEXT_SELECTOR).all() if n.is_visible()]
    choices = [n for g in item.root for n, _ in visible_options(g)]
    for control in scope.locator(ANSWER_SELECTOR).all():
        if not control.is_visible():
            if control.evaluate("e=>e.matches('input:not([type]),input[type=text],textarea,select')"):
                raise ManualRequired('小测还有隐藏的答题空格，请展开后截图。')
            continue
        if not any(control.evaluate('(e,n)=>e===n', n.element_handle()) for n in known+choices):
            raise ManualRequired('截图中的部分答题控件尚未适配，无法可靠填入答案。')
    if len(visible_options(scope)) != len(choices):
        raise ManualRequired('还有未关联题目的选项，不能提交部分答案。')
    for node in scope.locator(SOURCES + ', ' + TARGETS).all():
        if node.is_visible() and not any(is_drag_group(group) and group.evaluate(
                '(e,c)=>e.contains(c)', node.element_handle()) for group in item.root):
            raise ManualRequired('还有未能关联来源与落点的拖拽控件，不能提交部分答案。')
    if scope.locator('iframe:visible, frame:visible, video:visible, object:visible, embed:visible').count():
        raise ManualRequired('题目包含嵌入页面或视频，截图不能提供完整材料。')
    for canvas in scope.locator('canvas:visible').all():
        if not canvas.evaluate('e=>{try{e.toDataURL();return true}catch(_){return false}}'):
            raise ManualRequired('题目画布无法复核内容变化，暂不能自动提交此题。')
    for role in ('button', 'link'):
        if any(n.is_visible() and n.is_enabled() for n in item.frame.get_by_role(role, name=QUESTION_PAGE).all()):
            raise ManualRequired('当前测验分多页显示，请展开完整试卷；滚动区域支持自动截图。')
    return scope


def _visual_blank(control, identity):
    if control.evaluate("e=>e.tagName!=='SELECT'"):
        return _blank(control, identity)
    if not control.is_enabled() or control.get_attribute('multiple') is not None:
        raise ManualRequired('下拉控件不可操作或使用多选下拉，尚不能绑定答案。')
    values = {}
    for node in control.locator('option').all():
        data = node.evaluate('e=>({value:e.value,disabled:e.disabled||!!e.closest("optgroup[disabled]")})')
        if data['disabled'] or not data['value']:
            continue
        if data['value'] in values.values():
            raise ManualRequired('下拉选项值重复，无法唯一对应答案。')
        key = f'opt_{len(values)}'
        values[key] = data['value']
    if not values:
        raise ManualRequired('下拉选项尚未加载。')
    return QuizBlank(identity, list(values), input_type='select'), BlankBinding(identity, control, 'select', values)


def _page_letters(choices):
    # 仅绑定控件上单独显示的字母。选项正文始终留在截图里。
    labels = [option.locator('.num_option:visible,.num_option_dx:visible').evaluate_all(
        """nodes => {
          if (!nodes.length) return null;
          const label = nodes[0].innerText.trim();
          return nodes.length === 1 && /^[A-Z]$/.test(label) ? label : '';
        }""") for option, _ in choices]
    if all(label is None for label in labels):
        return []
    if not all(labels) or len(set(labels)) != len(labels):
        raise ManualRequired('本题选项字母缺失或重复，无法核对网页选择。')
    return labels


def _annotate(group, png, marks):
    """把控件编号画在截图外侧，题目像素保持原样。"""
    rectangles = []
    for identity, node in marks:
        handle = node.element_handle() if hasattr(node, 'element_handle') else node
        try:
            rect = group.evaluate('''(e,n)=>{const a=e.getBoundingClientRect(),b=n.getBoundingClientRect();
              return {x:b.left-a.left,y:b.top-a.top,w:b.width,h:b.height}}''', handle)
        finally:
            if hasattr(node, 'element_handle'):
                handle.dispose()
        rectangles.append(dict(id=identity, **rect))
    encoded = group.page.evaluate('''async ({data,marks})=>{
      const i=new Image();i.src='data:image/png;base64,'+data;await i.decode();
      const c=document.createElement('canvas');c.width=i.width+150;c.height=Math.max(i.height,marks.length*22);
      const g=c.getContext('2d');g.fillStyle='white';g.fillRect(0,0,c.width,c.height);g.drawImage(i,150,0);
      g.font='bold 12px Arial';g.strokeStyle='#c21d3a';g.fillStyle='#c21d3a';g.lineWidth=1;
      let last=-16;
      for(const m of [...marks].sort((a,b)=>a.y-b.y||a.x-b.x)){
        const y=Math.max(last+22,Math.min(c.height-(marks.length*22),m.y+m.h/2));last=y;
        g.fillText(m.id,4,y+14);g.beginPath();g.moveTo(88,y+10);g.lineTo(m.x+150,m.y+m.h/2);g.stroke();
        g.strokeRect(m.x+149,m.y-1,Math.max(4,m.w)+2,Math.max(4,m.h)+2);
      }
      return c.toDataURL('image/png').split(',')[1];
    }''', dict(data=base64.b64encode(png).decode('ascii'), marks=rectangles))
    result = base64.b64decode(encoded, validate=True)
    if len(result) > 4*1024*1024:
        raise ManualRequired('标注后的题目截图超过 4 MiB，无法发送完整截图。')
    return result


@contextmanager
def _capture_named(node, label):
    with expanded_region(node, label):
        try:
            yield
        except ManualRequired as exc:
            raise ManualRequired(f'{label}：{exc}') from exc


def read_screenshots(item, *, capture=True, check=lambda: None, progress=lambda message: None):
    scope = check_structure(item)
    before = fingerprint(item)
    shared = []
    audio_by_question = [[] for _ in item.root]
    if capture:
        for ai, audio in enumerate(scope.locator('audio').all()):
            if audio.evaluate('(e,s)=>!!e.closest(s)', EXCLUDED):
                continue
            owners = [i for i,g in enumerate(item.root) if g.evaluate('(e,n)=>e.contains(n)', audio.element_handle())]
            if not owners:
                if not audio.evaluate('(e,s)=>!!e.closest(s)', ', '.join((PASSAGE_SELECTOR, INSTRUCTION_SELECTOR))):
                    raise ManualRequired('音频不在明确题目材料区域，无法确认其对应题目。')
                owners = range(len(item.root))
            data, fmt = audio_bytes(audio)
            for owner in owners:
                audio_by_question[owner].append(QuizAudio(f'audio_{ai}', data, fmt))
        for i, node in enumerate(material_regions(item)):
            check()
            progress(f'正在截取小测第 {i+1} 份共用材料')
            with _capture_named(node, f'第 {i+1} 份材料'):
                shared.append(QuizImage(f'material_{i}', capture_region(node, check)))
    questions, bindings = [], []
    for index, group in enumerate(item.root):
        check()
        marks = []
        choices = visible_options(group)
        controls = [n for n in group.locator(TEXT_SELECTOR).all() if n.is_visible()]
        if is_drag_group(group):
            question, binding = read_drag(group, index, '', [], visual=True)
            marks = list(binding.sources.items()) + list(binding.targets.items())
        elif controls and not choices:
            pairs = [_visual_blank(n, f'q{index}_b{i}') for i,n in enumerate(controls)]
            question = QuizQuestion(str(index), 'fill', '直接看本题截图作答，红色编号对应填空位置。', [],
                                    blanks=[p[0] for p in pairs])
            binding = FillBinding([p[1] for p in pairs])
            marks = [(p[0].id, n) for p,n in zip(pairs, controls)]
            for blank, n in zip(binding.blanks, controls):
                if blank.input_type == 'select':
                    for option in n.locator('option').all():
                        value = option.evaluate('e=>e.value')
                        ids = [key for key, val in blank.option_values.items() if val==value]
                        if ids:
                            marks.append((blank.id+':'+ids[0], option))
        elif choices and not controls:
            kinds = [n.get_attribute('type') or n.get_attribute('role') for n,_ in choices]
            if len(choices)<2 or len(set(kinds))!=1 or any(not target.is_enabled() for _,target in choices):
                raise ManualRequired('题目选项未完整加载或不可操作，请稍后截图。')
            letters = _page_letters(choices)
            kind = 'multiple' if kinds[0] == 'checkbox' else 'single'
            if letters:
                question = QuizQuestion(str(index), kind,
                    f'看第 {index+1} 道题的专属题图 question_{index}，按原图字母返回 selected_labels。',
                    letters, page_letters=True)
            else:
                question = QuizQuestion(str(index), kind,
                    '直接看本题截图作答；selected 返回红色 opt 编号的整数，编号从 0 开始。',
                    [f'opt_{i}（含义见截图）' for i in range(len(choices))])
                marks = [(f'opt_{i}', target) for i, (_, target) in enumerate(choices)]
            binding = choices
        else:
            raise ManualRequired('本题控件混合或无法绑定，截图答题暂不支持此结构。')
        question.context = ('题干、选项、阅读材料和词库均在截图中，不提供文字提取。'
            '不要返回文字转录，直接按题意返回答案。截图缺少材料、个人自评或无法确定时，confidence 设为 0。'
            '红色标注只表示控件编号；绝不是答案。下拉题 filled 使用对应 opt_编号，不返回选项原文。')
        if capture:
            progress(f'正在截取小测第 {index+1} / {len(item.root)} 题')
            with _capture_named(group, f'第 {index+1} 题'):
                png = capture_region(group, check)
                if marks:
                    png = _annotate(group, png, marks)
            question.images = [QuizImage(f'question_{index}', png)] + shared
            question.audio = audio_by_question[index]
        questions.append(question)
        bindings.append(binding)
    if capture and fingerprint(item) != before:
        raise ManualRequired('截图期间题目内容已变化，请等待页面稳定后继续。')
    return questions, bindings


def structure_report(frame):
    groups = discover_groups(frame)
    if not groups:
        return None
    item = SimpleNamespace(root=groups, frame=frame)
    report = dict(questions=len(groups), blanks=0)
    try:
        questions, _ = read_screenshots(item, capture=False)
        report.update(status='supported', kinds=sorted({q.kind for q in questions}),
            blanks=sum(len(q.blanks) for q in questions),
            message='已识别答题控件；运行时直接截图交给图片模型，不使用本机文字识别。')
    except ManualRequired as exc:
        report.update(status='manual', message=str(exc))
    return report
