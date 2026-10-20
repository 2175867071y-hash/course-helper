"""读取结构明确的测验和媒体；答案数据与本地网页控件绑定分离。"""
from dataclasses import dataclass
import re

from browser import ManualRequired
from quiz_ai import QuizBlank, QuizQuestion
from quiz_drag import is_drag_group, read_drag, SOURCES, TARGETS
from quiz_media import collect_media, references, media_nodes


QUESTION_GROUPS = 'fieldset, .TiMu, .question-item, .questionLi, [data-question-id]'
OPTION_SELECTOR = 'input[type=radio], input[type=checkbox], [role=radio], [role=checkbox]'
TEXT_SELECTOR = 'input:not([type]), input[type=text], textarea, select'
ANSWER_SELECTOR = ('input:not([type=hidden]):not([type=button]):not([type=submit])'
                   ':not([type=reset]), textarea, select, [contenteditable=true], '
                   '[contenteditable=""], [contenteditable=plaintext-only], [role=textbox], '
                   '[role=combobox], [role=slider], [role=spinbutton]')
BANK_SELECTOR = '[data-word-bank], .word-bank, .wordBank, .wordbank, .phrase-bank'
PASSAGE_SELECTOR = '[data-passage], .reading-passage, .passage, .reading-material, .material-content, article'
INSTRUCTION_SELECTOR = '[data-instructions], .instructions, .question-instructions, .directions, .abs-direction'
QUESTION_PAGE = re.compile(r'^\s*(?:下一题|上一题|下题|下一页|上一页|'
    r'(?:next|previous)\s+(?:question|page))\s*$', re.I)
MATERIAL_DEPENDENCY = re.compile(r'\bpassage\b|\breading comprehension\b|'
    r'\b(?:read|according to)\b[^\n]{0,70}\b(?:text|article|dialogue|conversation)\b|'
    r'根据[^\n]{0,30}(?:材料|文章|短文|对话)|阅读[^\n]{0,20}(?:文章|短文|材料)|上述材料|下文', re.I)
AUDIO_DEPENDENCY = re.compile(r'\b(?:listen|listening|audio|recording)\b|听力|录音|跟读|口语', re.I)
BANK_DEPENDENCY = re.compile(r'\b(?:word|phrase)\s*bank\b|'
    r'\b(?:words?|phrases?|expressions?)\b[^\n]{0,40}\b(?:box|below|given)\b|词库|词组库|方框中', re.I)
PERSONAL = re.compile(r'self[ -]assessment|personal (?:feelings|experience)|'
    r'\brate\b[^\n]{0,250}\byour opinion\b|'
    r'(?:your|own) feelings|your own (?:life|experience|experiences|memories)|'
    r'(?:describe|tell|write)[^\n]{0,100}(?:your (?:life|experience|experiences)|about yourself)|'
    r'按.*(?:实际情况|自身感受)|自我评估|个人感受|自评|描述.*亲身经历|自己的经历', re.I)
TEXT_RULE = re.compile(r'\b(?:tense|voice|plural|singular|word limit|word count)\b|'
    r'\b(?:correct|proper|appropriate|past|present|future)\s+(?:form|forms|tense)\b|'
    r'\b(?:no more than|at most|at least|exactly)\s+\d+\s+words?\b|'
    r'时态|语态|词形|单复数|字数|(?:不超过|至少|最多)\s*\d+\s*(?:字|词)', re.I)
INSTRUCTION_HINT = re.compile(r'^\s*(?:instructions?|directions?|use|fill|complete|choose|'
    r'write|answer|change|put|select|supply|give|read)\b|^\s*(?:说明|要求|请|使用|用|填写|完成)', re.I)


@dataclass
class BlankBinding:
    id: str
    control: object
    input_type: str
    option_values: dict


@dataclass
class FillBinding:
    blanks: list


def discover_question_groups(frame):
    """保留明确题组；按答题说明、表头及已知答题区识别结构化填空。"""
    candidates = frame.locator(QUESTION_GROUPS + ', tr, p, li')
    indexes = candidates.evaluate_all(r"""(nodes, selectors) => {
        const visible = e => !!e.getClientRects().length &&
            getComputedStyle(e).visibility !== 'hidden' && !e.closest('[hidden]');
        const legacy = nodes.filter(e => e.matches(selectors.groups) && visible(e) &&
            ![...e.querySelectorAll(selectors.groups)].some(visible));
        const contexts = new Map();
        const hasInstructions = scope => {
            if (!scope) return false;
            if (!contexts.has(scope)) {
                contexts.set(scope, [...scope.querySelectorAll(selectors.instructions + ',p')].some(e => {
                    if (!visible(e) || e.querySelector(selectors.answers) ||
                        e.closest('aside,nav,header,footer,[role=search]')) return false;
                    const text = (e.innerText || '').trim();
                    return (e.matches(selectors.instructions) || /^(?:Directions?|Instructions?)\s*:/i.test(text)) &&
                        /\b(?:blanks?|questions?|exercises?|sentences?)\b|填空|答题|选择题|练习/i.test(text);
                }));
            }
            return contexts.get(scope);
        };
        const fallback = nodes.filter(e => {
            if (!visible(e) || e.matches(selectors.groups) ||
                e.closest('aside,nav,header,footer,[role=search]') ||
                !e.closest('main,[role=main],form,[role=dialog],.fill-blank-reply') ||
                !e.querySelector(selectors.answers) ||
                legacy.some(parent => parent.contains(e) || e.contains(parent))) return false;
            const text = (e.innerText || '').trim();
            if (!/[A-Za-z\u3400-\u9fff]{2}/.test(text)) return false;
            const numbered = /^\s*(?:\d+[.、)）]|[（(]\d+[)）])/.test(text);
            const instructions = hasInstructions(e.closest('form,[role=dialog]') ||
                                                 e.closest('main,[role=main]'));
            if (e.tagName === 'TR') {
                const table = e.closest('table');
                const headings = [...table.rows].filter(row => !row.querySelector(selectors.answers))
                    .map(row => row.innerText || '').join(' ');
                if (!e.closest('.fill-blank-reply') && !instructions &&
                    !/\b(?:questions?|answers?|sentences?|statements?|your score)\b|题目|题干|答案/.test(
                        headings.toLowerCase())) return false;
                return e.cells.length >= 2 && [...e.cells].some(cell =>
                    !cell.querySelector(selectors.answers) && (cell.innerText || '').trim().length >= 2);
            }
            return !!e.closest('.fill-blank-reply') || (numbered && instructions);
        });
        // 表格行内的段落不拆开，保留左列题干和右列空格的对应关系。
        const selected = [...legacy, ...fallback.filter(e =>
            !fallback.some(parent => parent !== e && parent.contains(e)))];
        return nodes.flatMap((e, index) => selected.includes(e) ? [index] : []);
    }""", {'groups': QUESTION_GROUPS, 'answers': ANSWER_SELECTOR + ', ' + OPTION_SELECTOR,
           'instructions': INSTRUCTION_SELECTOR})
    return [candidates.nth(index) for index in indexes]


def task_identity(frame):
    """只读取当前任务标签，防止同 URL 下异步解题结果落到另一 Task。"""
    return frame.locator('.pc-header-tasks-row').evaluate_all("""rows => rows.map(row =>
        [...row.querySelectorAll('.pc-task')].flatMap((e, index) =>
            e.getClientRects().length && e.classList.contains('pc-header-task-activity')
                ? [[index, (e.innerText || '').trim()]] : [])).filter(row => row.length)""")


def personal_notice(item, scope):
    text = scope.inner_text()
    scale = 'strongly agree' in text.lower() and 'strongly disagree' in text.lower()
    if not (PERSONAL.search(text) or scale):
        return ''
    count = sum(sum(control.is_visible() for control in group.locator(TEXT_SELECTOR).all())
                for group in item.root)
    # 量表范围取自可见说明，不假设所有自评题都是 1—7 分。
    points = re.findall(r'\b(\d+)\s*=\s*(?:strongly|somewhat|agree|disagree|neutral)', text, re.I)
    rating = f'（评分范围 {min(map(int, points))}—{max(map(int, points))}）' if len(set(points)) >= 2 else ''
    return (f'已识别 {len(item.root)} 道题、{count} 个填空；这是个人自评题{rating}，'
            '请按实际情况填写并提交，进入下一任务后点“已处理，继续”。AI 不会编造个人感受或经历。')


def table_context(group):
    """保留表头及当前行之前的共用题干，不采集其他行已填的答案。"""
    if group.evaluate("e => e.tagName === 'TR'"):
        _material_text(group.locator('xpath=ancestor::table[1]'))
    return group.evaluate("""(e, answers) => {
        if (e.tagName !== 'TR') return '';
        const table = e.closest('table');
        const context = [];
        if (table.caption) context.push((table.caption.innerText || '').trim());
        for (const row of table.rows) {
            if (row === e) break;
            if (row.closest('table') !== table || row.querySelector(answers) ||
                !row.getClientRects().length || getComputedStyle(row).visibility === 'hidden') continue;
            const text = [...row.cells].map(cell => (cell.innerText || '').trim()).join(' | ');
            if (text.trim()) context.push(text);
        }
        return context.length ? '表格说明/表头：\\n' + context.join('\\n') : '';
    }""", ANSWER_SELECTOR + ', ' + OPTION_SELECTOR)


def quiz_report(frame):
    """只报告结构与原因，不导出题干、答案或页面中的个人资料。"""
    from types import SimpleNamespace
    groups = discover_question_groups(frame)
    if not groups:
        return None
    item = SimpleNamespace(root=groups, frame=frame)
    report = {'questions': len(groups), 'blanks': sum(
        sum(control.is_visible() for control in group.locator(TEXT_SELECTOR).all()) for group in groups)}
    try:
        scope = quiz_scope(item)
        notice = personal_notice(item, scope)
        if notice:
            raise ManualRequired(notice)
        nodes = media_nodes(scope)
        if nodes:
            check_content(item, scope, multimedia=True)
            tags = [node.evaluate('e=>e.tagName.toLowerCase()') for node in nodes]
            pictures = sum(tag in ('img', 'svg', 'canvas') for tag in tags)
            sounds = tags.count('audio')
            report.update(status='media', images=pictures, audio=sounds,
                message=f'发现 {pictures} 张图片、{sounds} 段音频；运行时按图片模型/听力转写设置检查完整材料和控件。'
                        '检测阶段不上传或下载媒体，识别到媒体不代表已适配。')
            return report
        questions, _ = read_quiz(item)
        report.update(status='supported', kinds=sorted({q.kind for q in questions}),
                      message='已识别完整题目，可按 AI 小测设置处理。')
    except ManualRequired as exc:
        report.update(status='manual', message=str(exc))
    return report


def visible_options(group):
    """CSS 隐藏的选择框必须有可见 label 才能通过普通点击操作。"""
    result = []
    for option in group.locator(OPTION_SELECTOR).all():
        if option.is_visible():
            result.append((option, option))
            continue
        label = option.locator('xpath=ancestor::label[1]')
        if label.count() == 1 and label.is_visible():
            result.append((option, label))
            continue
        identity = option.get_attribute('id')
        if identity:
            labels = [item for item in group.locator('label').all()
                      if item.get_attribute('for') == identity and item.is_visible()]
            if len(labels) == 1:
                result.append((option, labels[0]))
    return result


def option_text(option):
    return option.evaluate("""e => {
        const labels = e.labels ? Array.from(e.labels) : [];
        return labels.map(l => l.innerText).join(' ').trim() ||
            e.getAttribute('aria-label') || e.innerText ||
            (e.closest('li') ? e.closest('li').innerText : '');
    }""").strip()


def prompt_text(group, ids=None):
    """按 DOM 文本顺序插入空格标识，不读取用户已填值或下拉全部选项。"""
    return group.evaluate("""(root, ids) => {
        let index = 0;
        const visible = e => !!e.getClientRects().length &&
            getComputedStyle(e).visibility !== 'hidden' && !e.closest('[hidden]');
        function read(node) {
            if (node.nodeType === Node.TEXT_NODE) return node.textContent;
            if (node.nodeType !== Node.ELEMENT_NODE || !visible(node) ||
                ['SCRIPT','STYLE','NOSCRIPT'].includes(node.tagName)) return '';
            if (node.matches('input:not([type]),input[type=text],textarea,select')) {
                const id = ids ? ids[index] : 'blank' + index;
                index++;
                return ' [[' + id + ']] ';
            }
            if (node.matches('input,button')) return '';
            const text = Array.from(node.childNodes).map(read).join('');
            return /^(block|flex|grid|list-item|table-row)$/.test(getComputedStyle(node).display)
                ? '\\n' + text + '\\n' : text;
        }
        return read(root).replace(/[ \\t]+/g,' ').replace(/ *\\n */g,'\\n')
            .replace(/\\n{3,}/g,'\\n\\n').trim();
    }""", ids)


def quiz_scope(item):
    for selector in ('xpath=ancestor::*[@role="dialog"][1]',
                     'xpath=ancestor::*[self::main or @role="main"][1]'):
        container = item.root[0].locator(selector)
        if container.count() == 1 and container.is_visible():
            if all(container.evaluate('(e, child) => e.contains(child)', group.element_handle())
                   for group in item.root):
                return container
    return item.frame.locator('body')


def _same_node(first, second):
    return first.evaluate('(e, other) => e === other', second.element_handle())


def _in_group(node, groups):
    return any(group.evaluate('(e, child) => e.contains(child)', node.element_handle())
               for group in groups)


def _material_text(node):
    if not node.is_visible():
        raise ManualRequired('题目材料尚未展开或不可见，请展开完整材料后继续。')
    incomplete = node.evaluate("""root => [root, ...root.querySelectorAll('*')].some(e => {
        const s = getComputedStyle(e);
        if (e.closest('script,style,noscript,template,[aria-hidden="true"]')) return false;
        const hiddenText = (e.textContent || '').trim() &&
            (e.hidden || s.display === 'none' || s.visibility === 'hidden');
        return e.getAttribute('aria-expanded') === 'false' ||
            hiddenText ||
            (e.tagName === 'DETAILS' && !e.open) ||
            ((s.overflowY === 'hidden' || s.overflowY === 'clip' ||
              (s.webkitLineClamp && s.webkitLineClamp !== 'none')) &&
             e.scrollHeight > e.clientHeight + 3);
    })""")
    if incomplete:
        raise ManualRequired('阅读材料或词库存在折叠、截断内容，请展开完整材料后继续。')
    text = node.inner_text().strip()
    if not text and node.locator('img,svg,canvas,audio').count():
        return '[题目媒体材料]'
    if not text:
        raise ManualRequired('阅读材料或词库尚未加载完整，请检查页面后继续。')
    return text


def shared_materials(item, scope):
    """只发送明确材料节点和相关说明，不把整个页面/账户文字送入 AI。"""
    passages, instructions, banks = [], [], []
    bank_groups = set()
    for node in scope.locator(PASSAGE_SELECTOR).all():
        # article 若包住整张试卷并不是可独立确认的阅读文章。
        if any(node.evaluate('(e,c)=>e.contains(c)', group.element_handle()) for group in item.root):
            continue
        if not any(parent.evaluate('(e, child)=>e.contains(child)', node.element_handle())
                   for parent in passages):
            _material_text(node)
            passages.append(node)
    instruction_nodes = []
    for node in scope.locator(INSTRUCTION_SELECTOR).all():
        _material_text(node)
        if not _in_group(node, item.root):
            instructions.append(node.inner_text().strip())
            instruction_nodes.append(node)
    # 常见纯文本说明没有 class；只收集与做题规则/材料依赖明确相关的段落。
    for node in scope.locator('p').all():
        if (not node.is_visible() or _in_group(node, item.root)
                or any(p.evaluate('(e, child)=>e.contains(child)', node.element_handle())
                       for p in passages + instruction_nodes)):
            continue
        text = node.inner_text().strip()
        if (MATERIAL_DEPENDENCY.search(text) or BANK_DEPENDENCY.search(text)
                or TEXT_RULE.search(text)
                or re.search(r'\b(?:each|every)\b.{0,40}\b(?:once|one time)\b|'
                             r'change.{0,20}forms?|只能用一次|可重复使用', text, re.I)):
            instructions.append(text)
        elif INSTRUCTION_HINT.search(text) and text not in instructions:
            raise ManualRequired('页面存在尚未识别的答题说明，不能省略规则后提交，请手动处理。')
    for node in scope.locator(BANK_SELECTOR).all():
        if any(is_drag_group(group) and group.evaluate('(e,c)=>e.contains(c)', node.element_handle())
               for group in item.root):
            continue
        _material_text(node)
        section = node.locator('xpath=ancestor::*[self::section or @data-question-set '
                               'or @data-quiz-group or @role="group"][1]')
        if section.count():
            bank_groups = {index for index, group in enumerate(item.root) if section.evaluate(
                '(e, child)=>e.contains(child)', group.element_handle())}
            if not bank_groups:
                raise ManualRequired('词库与题目位于不同题组，尚不能确认对应关系，请手动处理。')
        else:
            # 没有题组边界时仅将这一词库作为当前整页题组共享材料。
            bank_groups = set(range(len(item.root)))
        candidates = [child for child in node.locator('[data-word], .word-item, li').all()
                      if child.is_visible()]
        # 条目内部的修饰 span 不能拆成多个词；保留最外层语义条目的原文。
        children = [child for child in candidates if not any(
            not _same_node(parent, child) and parent.evaluate('(e,c)=>e.contains(c)', child.element_handle())
            for parent in candidates)]
        if not children:
            children = [child for child in node.locator(':scope > *').all() if child.is_visible()]
            direct_text = node.evaluate("e=>Array.from(e.childNodes).filter(n=>n.nodeType===3)"
                                        ".map(n=>n.textContent).join('').trim()")
            if children and direct_text:
                raise ManualRequired('词库条目混合了未分组文字，无法确认完整词组，请手动处理。')
        words = [child.inner_text().strip() for child in children] if children else [
            word.strip() for word in re.split(r'[\n;；]+', node.inner_text()) if word.strip()]
        if not words or any(not word for word in words):
            raise ManualRequired('无法完整拆分词库条目，请手动检查词库。')
        banks.append(words)
    if len(banks) > 1:
        raise ManualRequired('当前页存在多个词库，尚不能确认各题对应关系，请手动处理。')
    visible_text = scope.inner_text()
    if MATERIAL_DEPENDENCY.search(visible_text) and not passages:
        raise ManualRequired('题目依赖阅读材料，但未识别到完整文章，请手动展开材料或处理。')
    if BANK_DEPENDENCY.search(visible_text) and not banks and not all(is_drag_group(g) for g in item.root):
        raise ManualRequired('题目引用词库，但未能完整识别词库条目，请检查页面。')
    context = '\n\n'.join(dict.fromkeys(instructions + [p.inner_text().strip() for p in passages]))
    reuse = 'unspecified'
    if re.search(r'\b(?:each|every)\b(?:(?!more than|multiple times|at least).){0,40}\b(?:once|one time)\b|'
                 r'(?:每[个项词组]*|各).{0,12}(?:一次|一遍)|只能用一次',
                 '\n'.join(instructions), re.I):
        reuse = 'once'
    if re.search(r'can be (?:used|reused).{0,20}(?:more than once|multiple times)|'
                 r'may be (?:used|reused).{0,20}(?:more than once|multiple times)|可重复使用',
                 '\n'.join(instructions), re.I):
        if reuse == 'once':
            raise ManualRequired('词库复用说明存在歧义，请手动确认规则。')
        reuse = 'allowed'
    return context, banks[0] if banks else [], reuse, bank_groups


def check_content(item, scope, multimedia=False):
    for group in item.root:
        if group.evaluate("""e=>[e,...e.querySelectorAll('*')].some(n=>
            n.getClientRects().length && getComputedStyle(n).visibility!=='hidden' &&
            [null,'::before','::after'].some(p=>getComputedStyle(n,p).backgroundImage.includes('url(')))"""):
            raise ManualRequired('题目使用 CSS 背景图片，当前无法完整提取，请手动处理。')
    # 未验证的隐藏题可能是分页/折叠题；不能把当前可见部分当作完整试卷。
    # 只检查题容器，不误伤有可见 label 的 CSS 隐藏单选框。
    for group in scope.locator(QUESTION_GROUPS).all():
        if not group.is_visible() and group.locator(ANSWER_SELECTOR + ', ' + OPTION_SELECTOR).count():
            raise ManualRequired('小测还有隐藏或折叠的题目，无法确认试卷完整，请展开后处理。')
    known = [node for group in item.root for node in group.locator(TEXT_SELECTOR).all() if node.is_visible()]
    choices = [option for group in item.root for option, _ in visible_options(group)]
    for control in scope.locator(ANSWER_SELECTOR).all():
        if not control.is_visible():
            if control.evaluate("e => e.matches('input:not([type]),input[type=text],textarea,select')"):
                raise ManualRequired('小测还有隐藏的答题空格，无法确认题目完整，请展开后处理。')
            continue
        if not any(_same_node(control, node) for node in known + choices):
            raise ManualRequired('页面还有未能完整识别的答题控件，不能提交部分答案，请手动处理。')
    if len(visible_options(scope)) != len(choices):
        raise ManualRequired('页面还有未能完整识别的答题选项，请手动处理整个小测。')
    if scope.locator('iframe:visible, frame:visible').count():
        raise ManualRequired('小测包含嵌入页面，无法确认题目和材料完整，请手动处理。')
    if not multimedia and media_nodes(scope):
        raise ManualRequired('小测含图片或音视频媒体，当前文字 AI 无法完整读取，请手动处理。')
    for node in scope.locator(SOURCES + ', ' + TARGETS).all():
        if node.is_visible() and not any(is_drag_group(group) and group.evaluate(
                '(e,c)=>e.contains(c)', node.element_handle()) for group in item.root):
            raise ManualRequired('小测包含未能关联来源与落点的拖拽控件，请手动处理。')
    for role in ('button', 'link'):
        if any(button.is_visible() and button.is_enabled()
               for button in item.frame.get_by_role(role, name=QUESTION_PAGE).all()):
            raise ManualRequired('小测存在题目分页，无法确认已读取完整试卷，请手动处理。')
    if AUDIO_DEPENDENCY.search(scope.inner_text()) and not (multimedia and any(
            node.evaluate("e=>e.tagName==='AUDIO'") for node in media_nodes(scope))):
        raise ManualRequired('题目可能依赖听力或录音内容，请手动处理。')


def _blank(control, identity):
    if not control.is_enabled() or control.get_attribute('readonly') is not None:
        raise ManualRequired('题目输入控件尚不可填写，请检查页面状态。')
    tag = control.evaluate('e => e.tagName.toLowerCase()')
    max_length = control.get_attribute('maxlength')
    try:
        max_length = int(max_length) if max_length is not None else None
    except ValueError:
        raise ManualRequired('输入框的长度限制无法识别，请手动处理。') from None
    if max_length is not None and max_length < 0:
        max_length = None
    values = {}
    if tag == 'select':
        if control.get_attribute('multiple') is not None:
            raise ManualRequired('填空中存在多选下拉框，尚未适配，请手动处理。')
        for option in control.locator('option').all():
            data = option.evaluate('e => ({text:e.textContent.trim(), value:e.value, '
                                  'disabled:e.disabled || !!e.closest("optgroup[disabled]")})')
            if data['disabled'] or not data['value'] or re.fullmatch(
                    r'(?:请选择|选择|choose|select|please select)(?:[ .…-].*)?', data['text'], re.I):
                continue
            if not data['text'] or data['text'] in values or data['value'] in values.values():
                raise ManualRequired('下拉选项含空白或重复值，无法唯一对应答案，请手动处理。')
            values[data['text']] = data['value']
        if not values:
            raise ManualRequired('下拉框没有可识别的有效选项，请手动处理。')
    kind = tag if tag in ('textarea', 'select') else 'text'
    return (QuizBlank(identity, list(values), max_length, kind),
            BlankBinding(identity, control, kind, values))


def read_quiz(item, multimedia=False):
    scope = quiz_scope(item)
    notice = personal_notice(item, scope)
    if notice:
        raise ManualRequired(notice)
    check_content(item, scope, multimedia)
    media, mapping = collect_media(item, scope, multimedia)
    context, bank, reuse, bank_groups = shared_materials(item, scope)
    questions, bindings = [], []
    for index, group in enumerate(item.root):
        group_context = '\n\n'.join(part for part in (context, table_context(group)) if part)
        if is_drag_group(group):
            question, binding = read_drag(group, index, group_context, mapping)
            question.images, question.audio = media[index]
            questions.append(question)
            bindings.append(binding)
            continue
        choices = visible_options(group)
        controls = [control for control in group.locator(TEXT_SELECTOR).all() if control.is_visible()]
        if choices and controls:
            raise ManualRequired('同一道题混合了选择与填空控件，尚不能可靠对应，请手动处理。')
        if controls:
            pairs = [_blank(control, f'q{index}_b{number}') for number, control in enumerate(controls)]
            blanks, binding = [pair[0] for pair in pairs], FillBinding([pair[1] for pair in pairs])
            prompt = prompt_text(group, [blank.id for blank in blanks])
            group_bank = list(bank) if index in bank_groups else []
            question = QuizQuestion(str(index), 'fill', prompt, [], blanks, group_context, group_bank,
                                    reuse if group_bank else 'unspecified')
        else:
            if len(choices) < 2:
                raise ManualRequired('未能完整识别题目选项或输入空格，请手动处理。')
            texts = [option_text(option) for option, _ in choices]
            for number, (option, target) in enumerate(choices):
                refs = references(target, mapping)
                refs += [identity for media, identity in mapping if identity not in refs and option.evaluate(
                    '(e,m)=>[...(e.labels || [])].some(label=>label.contains(m))', media.element_handle())]
                if refs:
                    texts[number] += ' [图片：' + ', '.join(refs) + ']'
            kinds = [option.get_attribute('type') or option.get_attribute('role') for option, _ in choices]
            if any(not text for text in texts) or len(set(kinds)) != 1:
                raise ManualRequired('小测选项文字或控件结构不明确，请手动处理。')
            kind = 'multiple' if kinds[0] == 'checkbox' else 'single'
            normalized = {re.sub(r'^[A-Za-z][.、)）]\s*', '', text).strip().lower() for text in texts}
            if kind == 'single' and len(texts) == 2 and normalized in (
                    {'true', 'false'}, {'正确', '错误'}, {'对', '错'}, {'√', '×'}):
                kind = 'truefalse'
            prompt = prompt_text(group)
            question = QuizQuestion(str(index), kind, prompt, texts, context=group_context)
            binding = choices
        question.images, question.audio = media[index]
        if question.images:
            question.prompt += '\n题目相关图片：' + ', '.join(image.id for image in question.images)
            prompt = question.prompt
        if not prompt or len(prompt) > 20000:
            raise ManualRequired('小测题干为空或过长，无法确认内容完整，请手动处理。')
        questions.append(question)
        bindings.append(binding)
    if not questions:
        raise ManualRequired('没有找到可处理的文字题目。')
    return questions, bindings


def verify_fill(binding, filled):
    if set(filled) != {blank.id for blank in binding.blanks}:
        raise ManualRequired('答案与页面空格数量不一致，不会自动提交。')
    for blank in binding.blanks:
        expected = filled[blank.id]
        if blank.input_type == 'select':
            expected = blank.option_values.get(expected)
        if expected is None or blank.control.input_value() != expected:
            raise ManualRequired('网页未保留完整填写内容，请检查答案，不会自动提交。')
        if not blank.control.evaluate('e => e.validity.valid'):
            raise ManualRequired('填写内容未满足网页输入格式或长度约束，请检查答案，不会自动提交。')


def fill_text(binding, filled, check):
    for blank in binding.blanks:
        check()
        if blank.id not in filled:
            raise ManualRequired('AI 答案缺少空格，不会继续填写。')
        if blank.input_type == 'select':
            value = blank.option_values.get(filled[blank.id])
            if value is None:
                raise ManualRequired('答案不在当前下拉选项中，不会继续填写。')
            blank.control.select_option(value=value, timeout=5000)
        else:
            blank.control.fill(filled[blank.id], timeout=5000)
            blank.control.press('Tab', timeout=5000)
        check()
    verify_fill(binding, filled)
