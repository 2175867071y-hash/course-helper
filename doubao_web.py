"""豆包网页独立复核。只上传题图，读取结构化回答，不执行网页回答中的操作。"""
from contextlib import contextmanager
import json
import logging
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as BrowserTimeoutError
from quiz_ai import (QuizAIError, _SYSTEM_PROMPT, _load_json, _MAX_RESPONSE_BYTES,
                     SCREENSHOT_BATCH_SIZE, DOUBAO_BATCH_IMAGES)


DOUBAO_URL = 'https://www.doubao.com/chat/'
_EDITOR = '[contenteditable="true"]'
_SEND = '[data-testid="chat_input_send_button"]'
# 真实页面改版后 testid 可能变化；只作为定位线索，点击前仍要求唯一可点击。
_SEND_FALLBACKS = ('[data-testid*="send" i]',
                   '[aria-label*="发送"], [title*="发送"]',
                   '[aria-label*="send" i], [title*="send" i]',
                   'button[class*="send" i], [role="button"][class*="send" i]')
_CARDS = '[data-testid="attachment_area"] [data-testid="attachment-image-card"]'
_MESSAGES = '[data-message-role],[data-role="assistant"],[data-role="user"],[data-testid="receive_message"],[data-testid="send_message"],[data-message-id]'


def make_batches(data, images):
    """只在完整题目之间分批，共享限用词库必须保持全组约束。"""
    by_id = {image.id: image for image in images}
    batches, current, used = [], [], set()
    for item in data:
        required = set(item.get('images', []))
        if required - by_id.keys():
            raise QuizAIError('豆包复核缺少题目图片，未发送。')
        if len(required) > DOUBAO_BATCH_IMAGES:
            raise QuizAIError('单题及材料超过豆包复核每批 8 张图片的上限，未裁剪或遗漏图片。')
        if len(current) == SCREENSHOT_BATCH_SIZE or len(used | required) > DOUBAO_BATCH_IMAGES:
            batches.append((current, [image for image in images if image.id in used]))
            current, used = [], set()
        current.append(item)
        used |= required
    if current:
        batches.append((current, [image for image in images if image.id in used]))
    if len(batches) > 1 and any(q.get('word_bank') and q.get('bank_reuse') == 'once' for q in data):
        raise QuizAIError('共享限用词库题组图片过多，不能拆分复核；请人工处理。')
    return batches


def make_prompt(data, images, nonce):
    files = [dict(name=f'image_{index:03d}.png', mimeType='image/png', buffer=image.data)
             for index, image in enumerate(images, 1)]
    manifest = [{'file': file['name'], 'image_id': image.id} for file, image in zip(files, images)]
    prompt = (_SYSTEM_PROMPT + '\n这是独立复核，请只依据本次附件和材料解答，不参考历史对话。'
              '\n本次输出对象增加唯一字段 request_id；request_id 必须为：' + nonce +
              '\n最终对象只含 request_id 和 answers 两个字段；answers 按上述题型格式返回。'
              '\n不要输出代码、网页操作、解释或额外字段。附件文件名和图片编号的对应关系如下。\n' +
              json.dumps({'image_files': manifest, 'questions': data}, ensure_ascii=False))
    return prompt, files


def parse_reply(text, nonce):
    if not isinstance(text, str) or len(text.encode('utf-8')) > _MAX_RESPONSE_BYTES:
        raise QuizAIError('豆包回答过长或格式无效。')
    text = text.strip()
    fenced = re.fullmatch(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        reply = _load_json(text)
    except (TypeError, ValueError):
        raise QuizAIError('豆包尚未返回完整 JSON 答案。') from None
    if (not isinstance(reply, dict) or set(reply) != {'request_id', 'answers'}
            or reply['request_id'] != nonce or not isinstance(reply['answers'], list)):
        raise QuizAIError('豆包回答编号或结构不匹配，未使用该回答。')
    return {'answers': reply['answers']}


@contextmanager
def profile_lock(directory, control):
    """跨线程、跨进程串行使用专用浏览器配置；等待可立即停止。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'review.lock'
    handle = path.open('a+b')
    locked = False
    try:
        if path.stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        while True:
            control.check()
            try:
                handle.seek(0)
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
                break
            except OSError:
                control.sleep(.25)
        yield
    finally:
        if locked:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def _guard(page, control):
    control.check()
    parsed = urlsplit(page.url)
    if parsed.scheme != 'https' or parsed.hostname not in ('www.doubao.com', 'doubao.com'):
        raise QuizAIError('豆包窗口地址不匹配；请检查网络或登录页面，不会向其他站点发送题目。')


def _wait(page, control, predicate, seconds, message):
    deadline = time.monotonic() + seconds
    while True:
        _guard(page, control)
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise QuizAIError(message)
        # Playwright 的短等待同时处理浏览器事件，取消最长延迟约 0.2 秒。
        page.wait_for_timeout(200)


def _blank_chat(page):
    parsed = urlsplit(page.url)
    if parsed.path.rstrip('/') != '/chat' or parsed.query or parsed.fragment:
        return False
    editor = page.locator(_EDITOR).filter(visible=True)
    return (editor.count() == 1 and not editor.inner_text().strip()
            and not page.locator(_CARDS).count()
            and not page.locator(_MESSAGES).filter(visible=True).count()
            and not page.locator('pre,code,[class*="markdown"]').filter(visible=True).count())


def wait_new_chat(page, control, timeout=10):
    """首页地址、空编辑器和无历史消息同时成立，才允许发送。"""
    _wait(page, control, lambda: _blank_chat(page), timeout,
          '无法确认豆包已切换到空白新对话，未发送题目。')


def prepare_chat(page, control):
    _guard(page, control)
    if _blank_chat(page):
        return
    button = page.get_by_role('button', name=re.compile('新对话')).filter(visible=True)
    if button.count() != 1:
        raise QuizAIError('当前页不是空白新对话，且未找到唯一的新对话入口；未覆盖草稿或发送题目。')
    button.click(timeout=5000)
    wait_new_chat(page, control)


_REPLIES = r'''nonce => {
  const visible = e => !!(e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const buttons = [...document.querySelectorAll('button,[role="button"]')].filter(visible);
  const label = e => [e.getAttribute('aria-label'), e.getAttribute('title'),
    e.getAttribute('data-testid'), e.innerText].filter(Boolean).join(' ');
  const streaming = buttons.some(e => /stop|停止生成|停止回答|停止输出|停止响应|终止生成|^\s*停止\s*$/i.test(label(e)));
  const states = e => ['data-status','data-state','data-upload-status'].map(a=>e.getAttribute(a)||'');
  const blocked = e => states(e).some(s=>/^(loading|pending|uploading|streaming|generating|error|failed|cancelled|canceled|aborted|stopped)$/.test(s));
  const busy = e => {
    for (let p=e;p;p=p.parentElement) {
      if (p.getAttribute('aria-busy')==='true' || blocked(p)) return true;
    }
    return !!e.querySelector('[aria-busy="true"],[role="progressbar"]');
  };
  const USER = '[data-message-role="user"],[data-role="user"],[data-testid="send_message"]';
  const ASSISTANT = '[data-message-role="assistant"],[data-role="assistant"],[data-testid="receive_message"]';
  const hasCopy = e => [...e.querySelectorAll('button,[role="button"]')].some(b=>visible(b)&&/copy|复制/i.test(label(b)));
  const finished = e => states(e).some(s=>/^(complete|completed|finished|done|success)$/.test(s))
    || hasCopy(e) || !streaming;
  const found = [];
  const seen = new Set();
  // 自然语言回答通常不含 request_id；标准消息容器里的内容直接收录，
  // 没有容器时只收“像答案”的文本，避免把整页文字当成回复。
  const ANSWERISH = /答案|answer|选项|题号|解析/i;
  const consider = (text, requireHint) => {
    text = (text || '').trim();
    if (!text || text.length > 2000000 || seen.has(text)) return;
    if (requireHint && !text.includes(nonce) && !text.includes('"answers"') && !ANSWERISH.test(text)) return;
    seen.add(text);
    found.push({text, completed: true});
  };
  for (const leaf of document.querySelectorAll('pre,code,p,[data-testid*="message_text"],[class*="markdown"]')) {
    if (!visible(leaf) || leaf.closest('[contenteditable="true"]')) continue;
    if (leaf.closest(USER)) continue;
    const message = leaf.closest(ASSISTANT);
    if (message) {
      if (busy(message) || !finished(message)) continue;
      // 先收正文（代码块/文本块），再收整条消息；按钮文字不会干扰正文解析。
      const holder = leaf.closest('pre,code') || leaf;
      consider((holder.innerText || leaf.innerText || '').trim(), false);
      consider(message.innerText || '', false);
      if (found.length >= 30) return found;
      continue;
    }
    if (!(leaf.textContent || '').includes(nonce) && !ANSWERISH.test(leaf.textContent || '')) continue;
    // 页面改版后可能没有标准消息容器：直接看这段文本本身。
    // 未写完的 JSON 一定无法通过 request_id 校验，所以不会误用半截回答。
    const holder = leaf.closest('pre,code') || leaf;
    if (busy(leaf) || streaming) continue;
    consider((holder.innerText || leaf.innerText || '').trim(), true);
    if (found.length >= 30) return found;
  }
  return found;
}'''


# 真实附件卡片不一定带上传状态属性。这里同时收集状态属性、缩略图渲染情况与忙碌标记，
# 让调用方按证据强弱判断，而不是只认某一种 testid 属性。
_UPLOAD_STATE = r'''(args) => {
  const visible = e => !!(e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const states = e => ['data-upload-status','data-status','data-state']
    .map(a=>e.getAttribute(a)||'').filter(Boolean);
  const kind = values => {
    const joined = ' ' + values.join(' ').toLowerCase() + ' ';
    if (/[\s](error|failed|canceled|cancelled|aborted)[\s]/.test(joined)) return 'error';
    if (/[\s](success|complete|completed|uploaded|done)[\s]/.test(joined)) return 'ready';
    if (/[\s](loading|pending|uploading|processing)[\s]/.test(joined)) return 'busy';
    return '';
  };
  const editor = [...document.querySelectorAll('[contenteditable="true"]')].filter(visible)[0] || null;
  const area = document.querySelector('[data-testid="attachment_area"]');
  const text = area ? (area.innerText || '') : '';
  if (/上传失败|上传错误|文件过多|超过[^\n]{0,12}限制/.test(text)) {
    return {state: 'error', detail: text.slice(0, 120)};
  }
  let scope = area;
  if (!scope && editor) {
    // 有些版本没有 attachment_area，把附件直接放在输入区容器里。
    for (let p = editor; p && p !== document.body; p = p.parentElement) {
      if (p.querySelector('img[src^="blob:"],img[src^="data:"]')) { scope = p; break; }
    }
  }
  const pictures = scope ? [...scope.querySelectorAll('img')].filter(visible) : [];
  const loaded = pictures.filter(i => i.complete && i.naturalWidth > 0);
  const cards = [...document.querySelectorAll(args.cards)];
  const cardState = cards.map(card => {
    const name = card.getAttribute('aria-label') || '';
    let verdict = '';
    for (let p = card; p; p = p.parentElement) {
      if (p.getAttribute('aria-busy') === 'true') return {verdict: 'busy', name};
      const found = kind(states(p));
      if (found === 'error' || found === 'busy') return {verdict: found, name};
      if (found === 'ready' && !verdict) verdict = 'ready';
    }
    if (card.querySelector('[aria-busy="true"],[role="progressbar"]')) return {verdict: 'busy', name};
    const own = [...card.querySelectorAll('img')];
    if (own.length) {
      return {verdict: own.every(i => i.complete && i.naturalWidth > 0) ? 'ready' : 'busy', name};
    }
    return {verdict: verdict || 'unknown', name};
  });
  const busy = cardState.some(c => c.verdict === 'busy') ||
    !!(scope && scope.querySelector('[aria-busy="true"],[role="progressbar"]'));
  return {state: 'ok', detail: text.slice(0, 200), busy, names: cardState.map(c => c.name),
          cards: cardState.map(c => c.verdict), pictures: pictures.length, loaded: loaded.length};
}'''

_SEND_OK = r'''nonce => {
  const visible = e => !!(e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const editor = [...document.querySelectorAll('[contenteditable="true"]')].filter(visible)[0];
  const sel = '[data-message-role="user"],[data-role="user"],[data-testid="send_message"]';
  if ([...document.querySelectorAll(sel)].some(e => (e.innerText || '').includes(nonce))) return true;
  return !!editor && !(editor.innerText || '').trim();
}'''


def _upload_state(page):
    return page.evaluate(_UPLOAD_STATE, dict(cards=_CARDS))


def _uploaded(page, count):
    """缩略图已渲染、无忙碌标记都算完成；有失败标记立即报错，未知状态不放行。"""
    data = _upload_state(page)
    if data.get('state') == 'error':
        raise QuizAIError('豆包附件上传失败或超过网页限制，未发送题目。')
    if data.get('state') != 'ok' or data.get('busy'):
        return False
    verdicts = data.get('cards') or []
    if 'error' in verdicts:
        raise QuizAIError('豆包附件上传失败或超过网页限制，未发送题目。')
    if verdicts and len(verdicts) == count and all(value == 'ready' for value in verdicts):
        return True
    # 有些页面结构没有上传状态属性，以附件数量与缩略图渲染完成作为证据。
    return data.get('pictures', 0) >= count and data.get('loaded', 0) >= count


def _same_node(left, right):
    try:
        return bool(left.evaluate('(e, other) => e === other', other.element_handle()))
    except Exception:
        return False


def _send_target(page):
    """发送按钮可能改版；只在输入区容器内接受唯一可点击的候选。"""
    editor = page.locator(_EDITOR).filter(visible=True)
    scopes = []
    if editor.count() == 1:
        try:
            box = editor.locator('xpath=ancestor::*[not(self::body or self::html)][2]')
            if box.count() == 1:
                scopes.append(box)
        except Exception:
            pass
    scopes.append(page)
    for scope in scopes:
        for selector in (_SEND,) + _SEND_FALLBACKS:
            nodes = [node for node in scope.locator(selector).all() if node.is_visible()]
            unique = []
            for node in nodes:
                if not any(_same_node(node, other) for other in unique):
                    unique.append(node)
            if len(unique) > 1:
                return None
            if unique and unique[0].is_enabled():
                return unique[0]
    return None


def _prompt_sent(page, nonce):
    return bool(page.evaluate(_SEND_OK, nonce))


def _sent_within(page, nonce, control, seconds):
    deadline = time.monotonic() + seconds
    while True:
        control.check()
        if _prompt_sent(page, nonce):
            return True
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(200)


def _accepts_png(value):
    types = {part.strip().lower() for part in (value or '').split(',') if part.strip()}
    return not types or bool(types & {'*/*', 'image/*', 'image/png', '.png'})


def _composer_upload_input(page):
    # 只使用输入框与上传按钮共同容器内的文件控件，不扫描头像等全页上传入口。
    editor = page.locator(_EDITOR).filter(visible=True)
    root = editor.locator(
        'xpath=ancestor::*[not(self::body or self::html)]'
        '[.//*[@data-testid="upload_file_button"]][1]')
    if root.count() != 1:
        return None
    candidates = root.locator('input[type="file"]:not([disabled]):not([webkitdirectory])')
    usable = [candidates.nth(i) for i in range(candidates.count())
              if _accepts_png(candidates.nth(i).get_attribute('accept'))]
    if len(usable) > 1:
        raise QuizAIError('输入区有多个可用图片上传控件，无法确认目标，未上传题图。')
    return usable[0] if usable else None


def upload_images(page, files, control, mark, timeout):
    """同时支持加号直接选文件、上传菜单及输入区原生控件；只选择一次附件。"""
    choosers = []
    def on_chooser(chooser):
        choosers.append(chooser)
    def ready(allow_menu):
        if len(choosers) > 1:
            raise QuizAIError('同时出现多个文件选择器，无法确认目标，未上传题图。')
        if choosers:
            return 'chooser', choosers[0]
        native = _composer_upload_input(page)
        if native is not None:
            return 'input', native
        if allow_menu:
            menu = page.get_by_text(re.compile(r'^(上传文件或图片|上传图片|上传文件)$')).filter(visible=True)
            if menu.count() > 1:
                raise QuizAIError('出现多个图片上传菜单，无法确认目标，未上传题图。')
            if menu.count() == 1:
                return 'menu', menu
        return None
    # 必须在点击加号前监听；有些页面的加号直接触发文件选择器。
    page.on('filechooser', on_chooser)
    try:
        _guard(page, control)
        mark('打开附件上传入口')
        page.get_by_test_id('upload_file_button').click(timeout=5000)
        mark('选择上传文件或图片')
        kind, target = _wait(page, control, lambda: ready(True), min(timeout, 5),
            '已打开上传入口，但未找到图片上传菜单或输入区文件控件，未上传题图。')
        if kind == 'menu':
            _guard(page, control)
            mark('点击图片上传菜单')
            target.click(timeout=5000)
            mark('等待图片文件选择器')
            kind, target = _wait(page, control, lambda: ready(False), min(timeout, 5),
                '已点击图片上传菜单，但没有弹出文件选择器或出现输入区文件控件，未上传题图。')
        _guard(page, control)
        element = target.element if kind == 'chooser' else target
        if not _accepts_png(element.get_attribute('accept')):
            raise QuizAIError('当前文件选择器不接受 PNG 图片，未上传题图。')
        if element.evaluate('(e)=>e.disabled || e.webkitdirectory || e.files.length > 0'):
            raise QuizAIError('文件控件不可用或已有文件，未替换原附件。')
        multiple = target.is_multiple() if kind == 'chooser' else element.evaluate('(e)=>e.multiple')
        if len(files) > 1 and not multiple:
            raise QuizAIError('当前上传入口仅支持单文件，不能完整上传本批题图。')
        mark('选择题目图片')
        if kind == 'chooser':
            target.set_files(files, timeout=10000)
        else:
            target.set_input_files(files, timeout=10000)
    finally:
        page.remove_listener('filechooser', on_chooser)


def exchange(page, data, images, control, *, timeout=180, settle=3, progress=None, session=None):
    """仅在已准备好的新对话中发送一次；网络错误不会自动重复发送。"""
    _guard(page, control)
    wait_new_chat(page, control, min(timeout, 10))
    nonce = secrets.token_hex(16)
    prompt, files = make_prompt(data, images, nonce)
    if session is not None:
        session.update(nonce=nonce, prompt=prompt, data=data, image_count=len(files),
                       selected=False, format_sent=False)
    editor = page.locator(_EDITOR).filter(visible=True)
    if editor.count() != 1 or editor.inner_text().strip():
        raise QuizAIError('豆包新对话输入框未准备好或已有草稿，未覆盖原内容。')
    if page.locator(_CARDS).count():
        raise QuizAIError('豆包新对话存在旧附件，未发送题目。')
    mark = progress or (lambda stage: None)
    mark('填写豆包输入框')
    editor.fill(prompt, timeout=5000)
    if files:
        upload_images(page, files, control, mark, timeout)
        if session is not None:
            session['selected'] = True
        expected = len(files)
        stable_since = None
        # 短超时（自动化测试或网络很慢）时缩短稳定性要求，避免永远等不满。
        settle_time = min(1.0, max(0.2, timeout / 4))
        def uploaded():
            nonlocal stable_since
            if not _uploaded(page, expected):
                stable_since = None
                return False
            # 要求完成状态持续一小段时间，避免把刚渲染出的占位图当作上传完成。
            now = time.monotonic()
            if stable_since is None:
                stable_since = now
            return now - stable_since >= settle_time
        mark('等待附件上传完成')
        _wait(page, control, uploaded, min(timeout, 90), '豆包上传超时或附件不完整，未发送题目。')
    _send_only(page, editor, nonce, control, mark, timeout)
    mark('等待豆包回答')
    control.emit('status', message='题图已发送到豆包，等待独立复核答案…')
    formatted = False
    while True:
        try:
            return receive_reply(page, nonce, control, timeout=timeout, settle=settle,
                                  data=data)
        except QuizAIError as exc:
            # 豆包给了自然语言答案时，在同一对话里要求整理一次 JSON，不重传题图。
            if not getattr(exc, 'needs_format', False) or formatted:
                raise
            formatted = True
            control.emit('status', message='豆包未按 JSON 返回，正在同一对话要求整理一次格式…')
            nonce = _request_format(page, data, control, mark, timeout)
            mark('等待豆包整理后的答案')


def _send_only(page, editor, nonce, control, mark, timeout, stage='题目'):
    _guard(page, control)
    button = None
    def locate():
        nonlocal button
        button = _send_target(page)
        return button is not None
    mark('等待发送按钮')
    try:
        # 页面改版后按钮可能换名；先等十五秒，再改用回车键发送。
        _wait(page, control, locate, min(timeout, 15), '')
    except QuizAIError:
        button = None
    _guard(page, control)
    # 点击或回车都可能已经产生发送副作用；后续失败不能自动重发。
    mark('发送题目' if stage == '题目' else '发送答案整理请求')
    if button is not None:
        try:
            button.click(timeout=5000)
        except Exception:
            logging.warning('豆包发送按钮点击未完成；将尝试回车键一次。')
    if not _sent_within(page, nonce, control, min(timeout, 8) if button is not None else 0.5):
        mark('用回车发送题目' if stage == '题目' else '用回车发送整理请求')
        try:
            editor.press('Enter')
        except Exception:
            raise QuizAIError(f'豆包输入框无法发送{stage}，本批未发送。') from None
        if not _sent_within(page, nonce, control, min(timeout, 15)):
            raise QuizAIError(f'豆包没有接收已输入的{stage}：发送按钮和回车键都未生效，本批未发送。')


def _request_format(page, data, control, mark, timeout):
    """豆包只给了自然语言答案时，要求它按同一批题号整理为 JSON（不重传题图）。"""
    _guard(page, control)
    editor = page.locator(_EDITOR).filter(visible=True)
    if editor.count() != 1 or editor.inner_text().strip() or page.locator(_CARDS).count():
        raise QuizAIError('豆包输入区还有草稿或附件，未发送整理请求。')
    nonce = secrets.token_hex(16)
    prompt = (_SYSTEM_PROMPT + '\n请把上一次依据附件给出的答案整理为要求的 JSON。'
              '题图仍使用上一次的全部附件，不得根据不完整说明补猜；'
              '看不到全部选项或材料时 confidence 必须为 0。'
              '\n最终对象只能包含 request_id 和 answers。request_id 必须为：' + nonce +
              '\n题号和字段如下（不包含另一 AI 的答案）：\n' + json.dumps(data, ensure_ascii=False))
    mark('整理豆包回复格式（最多一次）')
    editor.fill(prompt, timeout=5000)
    _send_only(page, editor, nonce, control, mark, timeout, stage='整理请求')
    return nonce


_NUMBERED = re.compile(r'(?:^|\n)\s*(?:第\s*)?(\d{1,2})\s*(?:题|[、.．)）:：])')
_INLINE_NUMBERED = re.compile(r'(?:^|[\s，,；;：:、])(\d{1,2})\s*(?:[、.．)）:：])\s*')
_ANSWER_HINT = re.compile(r'(?:答案|正确答案|answer|应选|选择)\s*[:：]?\s*([^\n。；]{0,40})', re.I)
_LETTERS = re.compile(r'(?<![A-Za-z])([A-H]{1,8})(?![A-Za-z])')
_TRUE_WORDS = re.compile(r'(?:正确|对的|对|√|是|True|^T$)', re.I)
_FALSE_WORDS = re.compile(r'(?:错误|不对|错|×|否|False|^F$)', re.I)


def _split_numbered(text):
    blocks = []
    matches = list(_NUMBERED.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        blocks.append((int(match.group(1)), text[match.end():end]))
    if len(blocks) < 2:
        # 允许“1.D 2.B 3.ABCD”这种一行写完的回复。
        inline = list(_INLINE_NUMBERED.finditer(text))
        if len(inline) >= 2:
            blocks = []
            for index, match in enumerate(inline):
                end = inline[index + 1].start() if index + 1 < len(inline) else len(text)
                blocks.append((int(match.group(1)), text[match.end():end]))
    return blocks


def _answer_value(body):
    """从一段文字里取出答案：选项字母串，或判断题的对/错。"""
    if not body:
        return None
    hint = _ANSWER_HINT.search(body)
    target = (hint.group(1) if hint else re.split(r'[\n。；]', body, maxsplit=1)[0]).strip()
    if not target:
        return None
    # 先认字母（如“答案：A（对）”），再认判断题的对/错。
    match = _LETTERS.search(target)
    if match:
        return ''.join(match.group(1).upper())
    if _FALSE_WORDS.search(target) and not _TRUE_WORDS.search(target):
        return 'F'
    if _TRUE_WORDS.search(target) and not _FALSE_WORDS.search(target):
        return 'T'
    return None


def _loose_answer(item, value):
    kind = item.get('kind')
    options = item.get('options') or []
    letters_mode = item.get('answer_field') == 'selected_labels'
    if kind in ('drag', 'sort', 'fill') or not options:
        return None
    if value in ('T', 'F'):
        # 没有网页字母时无法确认“对/错”对应第几个选项，宁可放弃也不猜。
        if not letters_mode or len(options) != 2:
            return None
        letter = options[0] if value == 'T' else options[1]
        return {'id': item['id'], 'selected_labels': [letter], 'confidence': .9}
    letters = sorted(set(value))
    if letters_mode:
        if any(letter not in options for letter in letters):
            return None
        return {'id': item['id'], 'selected_labels': letters, 'confidence': .9}
    if len(letters) > len(options):
        return None
    indexes = sorted({ord(letter) - 65 for letter in letters})
    if any(index < 0 or index >= len(options) for index in indexes):
        return None
    return {'id': item['id'], 'selected': indexes, 'confidence': .9}


def parse_loose_reply(text, data):
    """把“1. 答案：D”“汇总：1.D 2.B”这类自然语言回答还原成结构化答案。

    只有题号与本批题目一一对应、且每题答案唯一时才采用；否则报错让调用方兜底。
    """
    blocks = _split_numbered(text or '')
    if not data or not blocks:
        raise QuizAIError('回答里没有可对应的题号。')
    seen = {}
    for number, body in blocks:
        value = _answer_value(body)
        if value is None:
            continue
        seen.setdefault(number, set()).add(value)
    if not seen:
        raise QuizAIError('回答里没有可识别的答案。')
    for offset in (1, 0):
        chosen = {}
        for index, item in enumerate(data):
            values = seen.get(index + offset)
            if not values or len(values) != 1:
                chosen = None
                break
            chosen[item['id']] = next(iter(values))
        if chosen is None:
            continue
        answers = []
        for item in data:
            answer = _loose_answer(item, chosen[item['id']])
            if answer is None:
                answers = None
                break
            answers.append(answer)
        if answers:
            return {'answers': answers}
    raise QuizAIError('回答的题号或选项无法与本批题目一一对应。')


def _looks_like_json(text):
    """看起来是（哪怕残缺的）JSON 时继续等，不触发格式整理。"""
    body = (text or '').strip()
    fenced = re.fullmatch(r'```(?:json)?\s*\n?(.*?)\n?```', body, re.S)
    if fenced:
        body = fenced.group(1).strip()
    return body.startswith('{') or '"answers"' in body


def receive_reply(page, nonce, control, *, timeout=180, settle=3, manual=False,
                   observe=None, data=None):
    """接收不再依赖上传调用栈；人工恢复仍须匹配本批请求编号。"""
    editor = page.locator(_EDITOR).filter(visible=True)
    last, since = None, time.monotonic()
    matched_seen = False
    def completed_reply():
        nonlocal last, since, matched_seen
        if manual:
            snapshot = page.evaluate(_CURRENT_TURN, nonce)
            if not snapshot['matched']:
                last, since = None, time.monotonic()
                return None
            matched_seen = True
            candidates = [{'text': snapshot['text'], 'completed': snapshot['completed']}]
        else:
            candidates = page.evaluate(_REPLIES, nonce)
        replies = {}
        plain = None
        for candidate in candidates:
            if not candidate['completed']:
                last, since = None, time.monotonic()
                continue
            text = candidate.get('text') or ''
            try:
                reply = parse_reply(text, nonce)
            except QuizAIError:
                # 完整的自然语言回答会触发一次格式整理；残缺 JSON 继续等。
                if text.strip() and not _looks_like_json(text):
                    plain = text
                continue
            replies[json.dumps(reply, ensure_ascii=False, sort_keys=True)] = reply
        # 输入框未清空、存在两份不同回答、或仍生成时，不采用任何一份。
        if len(replies) == 1 and not (editor.count() == 1 and editor.inner_text().strip()):
            key, reply = next(iter(replies.items()))
            if key != last:
                last, since = key, time.monotonic()
            return reply if time.monotonic() - since >= settle else None
        if replies:
            last, since = None, time.monotonic()
            return None
        if plain:
            if plain != last:
                last, since = plain, time.monotonic()
            if time.monotonic() - since >= settle:
                # 模型没按 JSON 回复时，先从题号文字里还原答案；能完整对应就采用。
                if data:
                    try:
                        loose = parse_loose_reply(plain, data)
                    except QuizAIError:
                        loose = None
                    if loose is not None:
                        logging.info('豆包未按 JSON 返回，已从文字回答解析出全部答案')
                        if observe:
                            observe(dict(status='已从豆包的文字回答解析出答案',
                                         raw_reply=plain[:12000]))
                        return loose
                if observe:
                    observe(dict(status='已收到普通回复，尚未通过答案格式校验',
                                 raw_reply=plain[:12000]))
                error = QuizAIError('豆包已回答，但没有按本批题号返回结构化答案。')
                error.needs_format = True
                raise error
            return None
        last, since = None, time.monotonic()
        return None
    try:
        return _wait(page, control, completed_reply, timeout,
                     '豆包回答超时或未返回可核验的完整答案；本组未提交。请检查豆包窗口、网络和登录状态。')
    except QuizAIError:
        if manual and not matched_seen:
            raise QuizAIError('未找到本批已发送的消息，不能接收其他对话的答案；请保留完整提示词和全部题图。') from None
        raise


_CURRENT_TURN = r'''nonce => {
  const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
  const userSelector = '[data-message-role="user"],[data-role="user"],[data-testid="send_message"]';
  const assistantSelector = '[data-message-role="assistant"],[data-role="assistant"],[data-testid="receive_message"]';
  const roots = [...document.querySelectorAll(userSelector+','+assistantSelector)]
    .filter(e=>!e.parentElement?.closest(userSelector+','+assistantSelector));
  const users = roots.filter(e=>e.matches(userSelector));
  const user = users.at(-1);
  const matched = !!user && (user.textContent||'').includes(nonce);
  const empty = {matched, text:'', completed:false};
  if (!matched) return empty;
  const replies = roots.slice(roots.indexOf(user)+1).filter(e=>e.matches(assistantSelector));
  if (replies.length !== 1) return empty;
  const reply = replies[0];
  const states = e => ['data-status','data-state'].map(a=>e.getAttribute(a)||'');
  const blocked = e => e.getAttribute('aria-busy')==='true' || states(e).some(s=>
    /^(loading|pending|streaming|generating|error|failed|cancelled|canceled|aborted|stopped)$/.test(s));
  for (let p=reply;p;p=p.parentElement) if(blocked(p)) return empty;
  if (reply.querySelector('[aria-busy="true"],[role="progressbar"]')) return empty;
  const controls = [...document.querySelectorAll('button,[role="button"]')].filter(visible);
  const label = e => [e.getAttribute('aria-label'),e.getAttribute('title'),e.getAttribute('data-testid'),e.innerText].filter(Boolean).join(' ');
  if (controls.some(e=>/stop|停止生成|停止回答|终止生成/i.test(label(e)))) return empty;
  // 人工恢复时，允许用该回答自己的“复制”操作表示输出完成，再等待文本稳定。
  const completed = states(reply).some(s=>/^(complete|completed|finished|done|success)$/.test(s)) ||
    [...reply.querySelectorAll('button,[role="button"]')].some(e=>visible(e)&&/copy|复制/i.test(label(e)));
  const body = reply.querySelector('pre,[data-testid*="message_text"],[class*="markdown"]') || reply;
  return {matched, text:(body.innerText||'').trim().slice(0,2000000), completed};
}'''


def resume_reply(page, session, control, mark, observe):
    """用户确认已完整手动发送后，仅接收本批；普通回复最多要求整理一次。"""
    _guard(page, control)
    nonce = session.get('reply_nonce', session['nonce'])
    mark('接收手动发送后的豆包回答')
    try:
        return receive_reply(page, nonce, control, manual=True, observe=observe,
                              data=session.get('data'))
    except QuizAIError as exc:
        if not getattr(exc, 'needs_format', False) or session['format_sent']:
            raise
    # 限定当前用户消息含本批编号，编辑器无草稿，不能发到其他会话。
    _guard(page, control)
    if not page.evaluate(_CURRENT_TURN, nonce)['matched']:
        raise QuizAIError('豆包会话已改变，未发送答案整理请求。')
    editor = page.locator(_EDITOR).filter(visible=True)
    if editor.count() != 1 or editor.inner_text().strip() or page.locator(_CARDS).count():
        raise QuizAIError('豆包输入区还有草稿或附件，未覆盖内容或发送整理请求。')
    # 整理请求另用编号，不能把前一次普通回答误认为新回复。
    nonce = secrets.token_hex(16)
    session['reply_nonce'] = nonce
    prompt = (_SYSTEM_PROMPT + '\n请把上一次依据附件给出的答案整理为要求的 JSON。'
        '题图仍使用上一次的全部附件，不得根据不完整说明补猜；看不到全部选项或材料时 confidence 必须为 0。'
        '\n最终对象只能包含 request_id 和 answers。request_id 必须为：' + nonce +
        '\n题号和字段如下（不包含另一 AI 的答案）：\n' + json.dumps(session['data'], ensure_ascii=False))
    mark('整理豆包回复格式（最多一次）')
    editor.fill(prompt, timeout=5000)
    _guard(page, control)
    session['format_sent'] = True  # 点击或回车即可能发送，失败也不能再发一遍。
    mark('发送答案整理请求')
    button = None
    def locate():
        nonlocal button
        button = _send_target(page)
        return button is not None
    try:
        _wait(page, control, locate, 10, '')
    except QuizAIError:
        button = None
    if button is not None:
        try:
            button.click(timeout=5000)
        except Exception:
            logging.warning('豆包整理请求的发送按钮点击未完成；将尝试回车键一次。')
    if not _sent_within(page, nonce, control, 8 if button is not None else 0.5):
        mark('用回车发送整理请求')
        try:
            editor.press('Enter')
        except Exception:
            raise QuizAIError('豆包输入框无法发送整理请求，本批未提交。') from None
        if not _sent_within(page, nonce, control, 15):
            raise QuizAIError('豆包没有接收到整理请求：发送按钮和回车键都未生效，本批未提交。')
    mark('等待豆包整理后的答案')
    return receive_reply(page, nonce, control, manual=True, observe=observe,
                          data=session.get('data'))


class DoubaoReviewer:
    def __init__(self, playwright, data_dir, channel, control):
        self.playwright = playwright
        self.directory = Path(data_dir) / 'doubao-review'
        self.channel = channel
        self.control = control
        self.observer = None

    def with_updates(self, data, images, observer):
        previous, self.observer = self.observer, observer
        try:
            return self(data, images)
        finally:
            self.observer = previous

    def _update(self, event):
        if self.observer is not None:
            self.observer(event)

    def _stage(self, stage):
        self.stage = stage
        if stage in ('发送题目', '发送答案整理请求'):
            self.sent = True
        message = '豆包复核：' + stage
        logging.info(message)
        self.control.emit('status', message=message)
        self._update(dict(status=stage))

    def _failure(self, original):
        """仅保存阶段和允许列出的错误码，绝不保存原始异常中的提示词、密钥或路径。"""
        if getattr(original, 'review_failure', False):
            return original
        codes = ('ERR_CONNECTION_REFUSED', 'ERR_CONNECTION_RESET', 'ERR_CONNECTION_CLOSED',
                 'ERR_NAME_NOT_RESOLVED', 'ERR_INTERNET_DISCONNECTED', 'ERR_TIMED_OUT',
                 'ERR_PROXY_CONNECTION_FAILED', 'ERR_TUNNEL_CONNECTION_FAILED',
                 'ERR_CERT_AUTHORITY_INVALID', 'ERR_CERT_DATE_INVALID', 'ERR_ABORTED')
        raw = str(original)
        code = next((value for value in codes if re.search(r'\b' + value + r'\b', raw)), '')
        kind = type(original).__name__
        if kind not in ('TimeoutError', 'TargetClosedError', 'Error', 'OSError',
                        'PermissionError', 'FileNotFoundError', 'QuizAIError'):
            kind = 'Exception'
        if isinstance(original, QuizAIError):
            detail = str(original)
        elif code:
            detail = code
        elif kind == 'TimeoutError':
            detail = '等待超时；请查看保留的豆包窗口。'
        elif kind == 'TargetClosedError':
            detail = '豆包窗口或浏览器已关闭。'
        elif self.stage == '启动豆包专用浏览器':
            detail = '浏览器未能启动；请检查所选浏览器是否可用、专用配置是否被另一实例占用。'
        else:
            detail = '网页操作未完成；请查看保留的窗口，确认对应入口或控件是否存在。'
        message = f'豆包复核失败，步骤：{self.stage}（{kind}）。{detail}'
        message += (' 本批发送状态需核对，不会自动重发。' if self.sent else ' 助手尚未点击发送；手动发送的结果需要继续接收。')
        record = dict(stage=self.stage, error_type=kind, code=code, sent=self.sent)
        try:
            (self.directory / 'last-error.json').write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError:
            pass
        logging.warning(message)
        failure = QuizAIError(message)
        failure.review_failure = True
        return failure

    def _exchange_with_recovery(self, page, questions, pictures, all_answers):
        session = {}
        try:
            return exchange(page, questions, pictures, self.control, progress=self._stage, session=session)
        except Exception as original:
            self.control.check()
            # 只有本批提示词已填入且题图已选择，或者已进入回答接收阶段，才提供恢复入口。
            recoverable = session.get('nonce') and (session.get('selected') or self.sent)
            if not recoverable or page.is_closed():
                raise
            failure = self._failure(original)
        while True:
            self._update(dict(status=str(failure), result={'answers': list(all_answers)}))
            choice = self.control.ask('豆包复核已暂停，可继续接收', str(failure) +
                f'\n本批应包含 {session["image_count"]} 张题图。'
                '\n如果你已在这个窗口完整发送本批提示词和全部题图，选择“已发送，接收回答”。'
                '助手会接收本批答案；普通文字回复会要求豆包整理一次格式，不重传题图。',
                [('receive', '已发送，接收回答'), ('close', '结束本次复核')], context='quiz')
            if choice != 'receive':
                failure.review_handled = True
                raise failure
            self.control.check()
            try:
                result = resume_reply(page, session, self.control, self._stage, self._update)
                self._update(dict(status='已收到豆包回答，正在核对题号',
                                  result={'answers': all_answers + result['answers']}))
                return result
            except Exception as original:
                self.control.check()
                failure = self._failure(original)
                if page.is_closed():
                    failure.review_handled = True
                    raise failure from None

    def _hold_failure_window(self, context, message):
        if context is None:
            return
        try:
            open_pages = [p for p in context.pages if not p.is_closed()]
        except Exception:
            return
        if not open_pages:
            return
        self.control.ask('豆包复核失败，窗口已保留', message +
            '\n这是网课助手专用的豆包窗口，与普通 Edge 的登录状态分开。'
            '\n请查看窗口；确认后关闭，本组不会填写或提交。',
            [('close', '已查看，关闭豆包窗口')], context='quiz')

    def __call__(self, data, images):
        batches = make_batches(data, images)
        self.control.emit('status', message='正在排队使用豆包网页复核…')
        context = None
        all_answers = []
        self.sent = False
        self.stage = '等待豆包专用配置'
        cancelled = False
        try:
            with profile_lock(self.directory, self.control):
                try:
                    self.control.check()
                    self._stage('启动豆包专用浏览器')
                    context = self.playwright.chromium.launch_persistent_context(
                        str(self.directory / f'profile-{self.channel}'), channel=self.channel,
                        headless=False, no_viewport=True, timeout=20000)
                    for index, (questions, pictures) in enumerate(batches, 1):
                        self.control.check()
                        self.sent = False
                        self._stage('创建豆包标签页')
                        page = context.new_page()
                        self._stage('打开豆包页面')
                        page.goto(DOUBAO_URL, wait_until='domcontentloaded', timeout=30000)
                        _guard(page, self.control)
                        self._stage('等待豆包输入框或登录')
                        try:
                            page.locator(_EDITOR).filter(visible=True).wait_for(timeout=10000)
                        except BrowserTimeoutError:
                            # 仅“未出现输入框”的超时需要人工登录，关闭窗口等异常不当作登录。
                            self.control.check()
                            answer = self.control.ask('登录豆包后继续',
                                '请在豆包专用窗口中自行登录，完成验证后继续。以后会沿用此登录状态。'
                                '\n若页面无法访问，请检查网络或 VPN 分流；助手不会修改系统代理。',
                                [('ready', '已登录，继续'), ('cancel', '取消本次复核')], context='quiz')
                            if answer != 'ready':
                                cancelled = True
                                raise QuizAIError('豆包复核已取消，本组未提交。')
                        _guard(page, self.control)
                        self._stage('准备空白新对话')
                        prepare_chat(page, self.control)
                        self.control.emit('status', message=f'豆包独立复核：第 {index}/{len(batches)} 批')
                        result = self._exchange_with_recovery(page, questions, pictures, all_answers)
                        self._stage('核对豆包回答题号')
                        # 先检查本批 ID，防止其他批次的答案被拼入。
                        ids = [a.get('id') if isinstance(a, dict) else None for a in result['answers']]
                        if (any(not isinstance(i, str) for i in ids) or len(ids) != len(questions)
                                or len(set(ids)) != len(ids) or set(ids) != {q['id'] for q in questions}):
                            raise QuizAIError('豆包本批回答题目编号不完整或不匹配，本组未提交。')
                        all_answers.extend(result['answers'])
                        page.close()
                    return {'answers': all_answers}
                except Exception as original:
                    self.control.check()
                    failure = self._failure(original)
                    failure.partial_result = {'answers': all_answers}
                    self._update(dict(status=str(failure), result={'answers': list(all_answers)}))
                    if not cancelled and not getattr(original, 'review_handled', False):
                        self._hold_failure_window(context, str(failure))
                    raise failure from None
                finally:
                    if context is not None:
                        try:
                            context.close()
                        except Exception:
                            # 清理异常不能覆盖实际失败步骤或已收到的完整答案。
                            logging.warning('豆包专用浏览器清理未完成；请手动关闭残留窗口。')
        except QuizAIError as exc:
            exc.partial_result = {'answers': all_answers}
            raise
        except Exception as original:
            self.control.check()
            exc = self._failure(original)
            exc.partial_result = {'answers': all_answers}
            raise exc from None
