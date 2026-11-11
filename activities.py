"""通过可见阅读器和题目控件处理文档、小测；不修改平台的完成状态。"""
from dataclasses import dataclass, replace
import hashlib
import logging
import re
import time
from urllib.parse import urlsplit

from browser import AUTH_RE, ManualRequired, only_one, page_identity
from quiz_ai import QuizAIError, QuizDisagreement, solve_questions
from quiz_drag import DragBinding, drag_prompt, is_drag_group, fill_drag, verify_drag
from quiz_dom import (FillBinding, discover_question_groups, task_identity, fill_text, prompt_text,
                      read_quiz, verify_fill, option_text as _option_text,
                      visible_options as _visible_options, quiz_result_scope, option_checked)
from dom_read import retry_dom_read
from playwright.sync_api import TimeoutError as ExactTimeoutError


DOC_SELECTOR = '#viewerContainer, .pdfViewer, [role=document], .document-viewer, .reader-container'
DOC_PATH = re.compile(r'/(?:pdf|ppt|document|read|doc)(?:/|\.)', re.I)
NEXT_PAGE = re.compile(r'^\s*(?:下一页|下页|Next\s+page)\s*$', re.I)
SUBMIT = re.compile(r'^\s*(?:提\s*交(?:\s*答案|\s*作业)?|交\s*卷|Submit(?:\s+(?:answers|quiz|test))?)\s*$', re.I)
SUCCESS = re.compile(r'^\s*(?:(?:(?:本次|当前)?(?:答案|作业|小测|测验|试卷))?(?:提交成功|已提交)(?:[，,]\s*感谢作答)?|'
    r'(?:作业|测验|小测)已完成|(?:本次得分|最终成绩)\s*[:：]\s*\d+(?:\.\d+)?(?:\s*/\s*\d+)?\s*分?|'
    r'(?:(?:your\s+)?(?:answers?|quiz|test)\s+)?submitted successfully|submission successful|'
    r'(?:quiz|test) completed|final score\s*:\s*\d+(?:\.\d+)?(?:\s*/\s*\d+)?)\s*[.!。！]?\s*$', re.I)
SUBMIT_FAILURE = re.compile(r'\b(?:not|never)\s+(?:been\s+)?(?:submitted|completed|successful)|'
    r'\b(?:failed|unsuccessful|failure|try again)\b|未(?:能|曾)?提交|未完成|未成功|失败|重试', re.I)
SUBMIT_DIALOG = ('[role=dialog], dialog[open], .layui-layer-dialog, '
                 '.el-message-box, .el-dialog, .ant-modal-content')
CONFIRM_BUTTON = re.compile(r'^\s*(?:确\s*定|确\s*认|确认提交|确定提交|继续提交|'
                            r'提\s*交|交\s*卷|OK|Yes|Confirm|Submit(?:\s+answers)?)\s*$', re.I)
# 未知前端框架的确认层：只在按钮文字和层内文字同时明确时点击。
CONFIRM_LAYER_JS = r'''({request, block, button, mark, known}) => {
  const requestRe = new RegExp(request, 'i');
  const blockRe = block ? new RegExp(block, 'i') : null;
  const buttonRe = new RegExp(button, 'i');
  const stripRe = /^\s*(?:确\s*定|确\s*认|取\s*消|提\s*交|交\s*卷|关\s*闭|返回|继续|重新作答|OK|Cancel|Yes|No|Confirm|Close|Submit(?:\s+answers)?)\s*$/i;
  const visible = e => {
    if (!e.getClientRects().length) return false;
    const s = getComputedStyle(e);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const labelOf = e => (e.tagName === 'INPUT' ? (e.value || '') : (e.innerText || '')).trim();
  // 超星等站点用自定义 div/span 做按钮，没有 button 标签也没有 onclick 属性。
  const BUTTON = 'button,[role="button"],a,input[type=button],input[type=submit],[onclick],'
    + '[class*="btn"],[class*="Btn"],[class*="button"],[class*="confirm"],[class*="submit"]';
  // 层文本要剔除按钮自己的文字，否则“取消”按钮会让正文永远像“含取消”。
  const bodyOf = box => {
    // 克隆后删掉按钮元素再取文本，避免把正文里的“提交”也一并剥掉。
    const copy = box.cloneNode(true);
    for (const node of copy.querySelectorAll(BUTTON)) {
      const t = labelOf(node);
      if (t && t.length <= 40 && stripRe.test(t)) node.remove();
    }
    return (copy.textContent || '').replace(/\s+/g, ' ').trim();
  };
  for (const old of document.querySelectorAll('[' + mark + ']')) old.removeAttribute(mark);
  const found = [];
  for (const b of document.querySelectorAll(BUTTON)) {
    if (!visible(b) || b.disabled || b.getAttribute('aria-disabled') === 'true') continue;
    const text = labelOf(b);
    if (!text || text.length > 40 || !buttonRe.test(text)) continue;
    let box = b.parentElement;
    for (let depth = 0; box && depth < 6 && box !== document.body; depth++, box = box.parentElement) {
      if ((box.innerText || '').trim().length >= 6) break;
    }
    if (!box || box === document.body) continue;
    const body = bodyOf(box);
    if (body.length > 600 || !requestRe.test(body)) continue;
    if (blockRe && blockRe.test(body)) continue;
    // 提交前就已经显示的提示层不属于本次交卷，不能拿它当确认框。
    if (b.closest('[data-ch-old]')) continue;
    if (box.querySelector('input:not([type=button]):not([type=submit]),textarea,select')) continue;
    found.push(b);
    if (found.length > 1) break;
  }
  if (found.length === 1) found[0].setAttribute(mark, '1');
  return {count: found.length};
}'''
PROMPT_TEXTS_JS = r'''(request) => {
  const requestRe = new RegExp(request, 'i');
  const stripRe = /^\s*(?:确\s*定|确\s*认|取\s*消|提\s*交|交\s*卷|关\s*闭|返回|继续|重新作答|OK|Cancel|Yes|No|Confirm|Close|Submit(?:\s+answers)?)\s*$/i;
  const visible = e => {
    if (!e.getClientRects().length) return false;
    const s = getComputedStyle(e);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const labelOf = e => (e.tagName === 'INPUT' ? (e.value || '') : (e.innerText || '')).trim();
  // 超星等站点用自定义 div/span 做按钮，没有 button 标签也没有 onclick 属性。
  const BUTTON = 'button,[role="button"],a,input[type=button],input[type=submit],[onclick],'
    + '[class*="btn"],[class*="Btn"],[class*="button"],[class*="confirm"],[class*="submit"]';
  const bodyOf = box => {
    // 克隆后删掉按钮元素再取文本，避免把正文里的“提交”也一并剥掉。
    const copy = box.cloneNode(true);
    for (const node of copy.querySelectorAll(BUTTON)) {
      const t = labelOf(node);
      if (t && t.length <= 40 && stripRe.test(t)) node.remove();
    }
    return (copy.textContent || '').replace(/\s+/g, ' ').trim();
  };
  for (const old of document.querySelectorAll('[data-ch-old]')) old.removeAttribute('data-ch-old');
  let count = 0;
  for (const b of document.querySelectorAll(BUTTON)) {
    if (!visible(b)) continue;
    let box = b.parentElement;
    for (let depth = 0; box && depth < 6 && box !== document.body; depth++, box = box.parentElement) {
      if ((box.innerText || '').trim().length >= 6) break;
    }
    if (!box || box === document.body) continue;
    const body = bodyOf(box);
    if (body.length <= 600 && requestRe.test(body)) { box.setAttribute('data-ch-old', '1'); count++; }
  }
  return count;
}'''
SUCCESS_PROMPT = re.compile(r'(?:提交成功|已提交|提交完成|已完成|已保存|submitted successfully|'
                            r'quiz completed|final score)', re.I)
SUCCESS_DISMISS = re.compile(r'^\s*(?:确定|确认|知道了|好的|好|关闭|返回|继续|OK|Got it|Close)\s*$', re.I)
CONFIRM_REQUEST = re.compile(r'(?:确定|确认|是否)(?:要|现在|立即|确认|提交|交卷)?(?:提交|交卷)|'
    r'(?:提交|交卷)[\s\S]{0,100}(?:吗|么|是否继续|是否确认)|'
    r'(?:are you sure|confirm)[\s\S]{0,100}(?:submit|turn in)|'
    r'(?:submit|turn in)[\s\S]{0,80}\?', re.I)
CONFIRM_BLOCK = re.compile(r'未(?:全部)?(?:作答|答题|完成|填写)|漏答|没有(?:作答|填写)|'
    r'删除|支付|购买|退出|重新作答|取消|放弃|不提交|撤回|unanswered|incomplete|not\s+(?:all\s+)?answered|'
    r'delete|purchase|payment|log\s*out', re.I)


def _submission_confirmation(text):
    """仅识别交卷弹窗提示，不读取题目文字。漏答或验证提示不能代为确认。"""
    return (isinstance(text, str) and len(text) <= 600 and
            not AUTH_RE.search(text) and not CONFIRM_BLOCK.search(text) and
            bool(CONFIRM_REQUEST.search(text)))

# 课件 iframe 嵌套极少超过此深度；上限用于防止非 Frame 对象（如测试替身）让向上遍历永不结束。
MAX_FRAME_DEPTH = 16

def _frame_chain(frame):
    """从 frame 逐级向上，最多 MAX_FRAME_DEPTH 层。"""
    depth = 0
    while frame is not None and depth < MAX_FRAME_DEPTH:
        yield frame
        frame = frame.parent_frame
        depth += 1

SCROLL_ROOT = """const scrollRoot=e=>{
    for (let n=e;n && n!==document.body;n=n.parentElement) {
        if (/auto|scroll/.test(getComputedStyle(n).overflowY) && n.scrollHeight>n.clientHeight+3) return n;
    }
    return document.scrollingElement;
};"""


@dataclass
class Activity:
    kind: str
    key: str
    frame: object
    root: object
    context: str = ''
    success_scope: object = None
    task_context: object = None
    receipts_before: frozenset = frozenset()


@dataclass
class ReviewedQuiz:
    key: str
    questions: list
    answers: list
    scope: str = ''


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:32]


class CourseActivities:
    def __init__(self, adapter, auto_documents=True, auto_quiz=True, document_seconds=10,
                 ai_base_url='', ai_model='', ai_api_key='', control=None, quiz_submit=True,
                 vision_model='', speech_base_url='', speech_model='', speech_api_key='', screenshot_quiz=False,
                 quiz_cross_check=False, ai_review_model='', web_review=None):
        self.adapter = adapter
        self.auto_documents = auto_documents
        self.auto_quiz = auto_quiz
        self.document_seconds = document_seconds
        self.ai_base_url = ai_base_url
        self.ai_model = ai_model
        self.ai_api_key = ai_api_key
        self.vision_model = vision_model
        self.screenshot_quiz = screenshot_quiz
        self.quiz_cross_check = quiz_cross_check
        self.ai_review_model = ai_review_model
        self.web_review = web_review
        self.speech_base_url = speech_base_url
        self.speech_model = speech_model
        self.speech_api_key = speech_api_key
        self.control = control
        self.quiz_submit = quiz_submit
        self.awaiting_manual_submit = set()
        self.document_due = {}
        self.document_end = {}
        self.document_steps = {}
        self.document_pending = {}
        self.submitted = {}
        # 交卷后页面出现成功提示会改变题目指纹；按“同一份试卷”记录提交时间和基线。
        self.submitted_attempts = {}
        self.success_baseline = {}
        self.quiz_attempts = set()
        self.completed = set()
        self.inline_completed = set()
        self.last_activity = None
        self.on_recognized = None
        self.pause_context = ''
        self.pending_quiz = None
        self.reviewed_quiz = None
        self.success_before = {}
        self.confirmation_key = None
        self.confirmation_before = []
        self.confirmation_frames = []
        self.confirmed = set()
        self.adapter.quiz_dialog_handler = self._on_submission_dialog
        # Adapter 用同一识别器判断“下一节”已转到文档/小测。
        self.adapter.activity_identity = self.identity
        self.adapter.course_handled = self._course_handled
        self.adapter.activity_completed = self._activity_completed

    def _course_handled(self):
        item = self._detect()
        return bool(item and item.key in self.inline_completed)

    def _activity_completed(self, key):
        """key 是否代表已提交或已完成的试卷；用于切下一节时识别结果页。"""
        return bool(key) and (key in self.completed or key in self.submitted)

    def _emit(self, message):
        logging.info('%s', message)
        if self.control is not None:
            self.control.emit('status', message=message)

    def _report(self, message):
        if self.control is not None:
            self.control.emit('quiz_report', message=message)

    def _check(self):
        if self.control is not None:
            self.control.check()
        reason = self.adapter.guard(include_course=False)
        if reason:
            raise ManualRequired(reason)

    def _groups(self, frame):
        if self.screenshot_quiz:
            from quiz_screenshots import discover_groups
            return discover_groups(frame)
        return discover_question_groups(frame)

    def _attempt(self, item):
        """同一份试卷的身份：重绘、作答状态变化不产生新身份。"""
        if self.screenshot_quiz:
            from quiz_screenshots import attempt_scope
            return attempt_scope(item)
        return item.key

    def _fingerprint(self, item):
        from quiz_screenshots import fingerprint
        return fingerprint(item)

    def _page_context(self):
        identity = getattr(self.adapter, 'content_identity', None)
        return identity() if callable(identity) else page_identity(self.adapter.page.url)

    @staticmethod
    def _static_roots(frame):
        """只接纳明确的纯阅读区域；未知表单、音频、画布不能当作读完。"""
        roots = []
        candidates = frame.locator('main, [role=main], article').all()
        for root in candidates:
            if not root.is_visible():
                continue
            valid = root.evaluate("""e => {
                if (e.querySelector('main,[role=main],article')) return false;
                const scope=e.closest('main,[role=main]') || e;
                if (scope.querySelector('video,audio,iframe,embed,object,canvas,form,input,textarea,select,'+
                    'button,[role=button],[role=radio],[role=checkbox],[role=combobox],'+
                    '[role=textbox],[role=spinbutton],[role=slider],'+
                    '[contenteditable]:not([contenteditable=false]),[aria-busy=true]')) return false;
                return (e.innerText||'').trim().length>=4 || !!e.querySelector('img');
            }""")
            if valid:
                roots.append(root)
        return roots

    @retry_dom_read
    def _detect(self):
        identity = self._page_context()
        frames = self.adapter.visible_frames()
        quizzes = []
        documents = []
        for frame in frames:
            groups = self._groups(frame)
            if groups:
                if self.screenshot_quiz:
                    from quiz_screenshots import fingerprint
                    item = Activity('quiz', '', frame, groups,
                        identity + '|' + page_identity(frame.url), task_context=task_identity(frame))
                    item.key = 'quiz:' + _digest(item.context + str(item.task_context) + fingerprint(item))
                    quizzes.append(item)
                    continue
                # 题干不含已选状态，填答案后身份保持不变。
                signatures = []
                for group in groups:
                    identifiers = '|'.join((option.get_attribute('id') or '') + ':' +
                                           (option.get_attribute('name') or '') + ':' +
                                           (option.get_attribute('type') or option.get_attribute('role') or '')
                                           + ':' + _option_text(option)
                                           for option, _ in _visible_options(group))
                    signatures.append((drag_prompt(group) if is_drag_group(group) else prompt_text(group)) + identifiers)
                tasks = task_identity(frame)
                text = str(tasks) + '|' + '|'.join(signatures)
                key = 'quiz:' + _digest(identity + page_identity(frame.url) + text)
                quizzes.append(Activity('quiz', key, frame, groups,
                                        identity + '|' + page_identity(frame.url), task_context=tasks))
                continue
            roots = [item for item in frame.locator(DOC_SELECTOR).all() if item.is_visible()]
            # PDF.js 外层滚动容器优先，避免把内部页列表识别成第二份文档。
            container = [item for item in roots if item.get_attribute('id') == 'viewerContainer']
            if container:
                roots = container
            if not roots and DOC_PATH.search(urlsplit(frame.url).path):
                body = frame.locator('body')
                if body.is_visible():
                    roots = [body]
            static = False
            # 新的静态阅读识别用于有明确内容语义的页面，不推断任意 div。
            if not roots and callable(getattr(self.adapter, 'content_identity', None)):
                roots = self._static_roots(frame)
                static = True
            for index, root in enumerate(roots):
                content = root.inner_text() if static else ''
                key = 'document:' + _digest(identity + page_identity(frame.url) + str(index) + content)
                documents.append(Activity('document', key, frame, root))
        # 弹出的测验优先于底下的课件/视频。
        if quizzes:
            return only_one(quizzes, '小测区域')
        return only_one(documents, '文档阅读器')

    def identity(self):
        item = self._detect()
        if item:
            # 交卷后页面会显示结果、指纹随之变化；同一份已提交的试卷保持稳定身份，
            # 否则 advance_activity 会把结果页误判成“已切换到新活动”而不切下一节。
            previous = self.last_activity
            if (item.kind == 'quiz' and previous is not None
                    and previous.kind == 'quiz'):
                attempt = self._attempt(item)
                if (attempt in self.submitted_attempts
                        and attempt == self._attempt(previous)):
                    return previous.key
            return item.key
        previous = self.last_activity
        if (previous and previous.key in self.completed and previous.context
                and previous.context.startswith(self._page_context() + '|')
                and not any(not video.state.ended for video in self.adapter.videos())):
            return previous.key
        return ''

    def advance_key(self):
        """刚刚完成的活动 key；页面已显示提交成功时也能用来切换下一节。"""
        if self.last_activity is not None:
            return self.last_activity.key
        return self.identity()

    def process(self):
        # 每次重新检测，登录、目录或视频提示不能继承上一组题目的上下文。
        self.pause_context = ''
        self._check()
        navigator = getattr(self.adapter, 'navigator', None)
        if navigator is not None:
            navigator.ensure_ready()
        now = time.monotonic()
        if self.pending_quiz is not None:
            self.pause_context = 'quiz'
            pending = self.pending_quiz
            current = self._detect()
            if current is None or current.kind != 'quiz':
                return self._quiz_after_form_removed(pending, current, now)
            changed_task = (pending.task_context is not None and
                            task_identity(pending.frame) != pending.task_context)
            if changed_task or (pending.context and not pending.context.startswith(
                    self._page_context() + '|')):
                self._clear_confirmation()
                self.pending_quiz = None
                self.completed.add(pending.key)
                raise ManualRequired('小测页面或 Task 已切换，请核对上一任务结果，再选择继续；不会重复提交。')
            self.last_activity = pending
            return self._quiz(pending, now)
        item = self._detect()
        self.last_activity = item
        if item is not None and self.on_recognized is not None:
            self.on_recognized()
        if item is not None and self.reviewed_quiz is not None:
            if item.kind != 'quiz' or item.key != self.reviewed_quiz.key:
                self.reviewed_quiz = None
        if item is None:
            return None
        if item.key in self.inline_completed:
            return None
        if item.key in self.completed:
            return 'done'
        if item.kind == 'document':
            return self._document(item, now)
        return self._quiz(item, now)

    def _quiz_after_form_removed(self, pending, current, now):
        submitted_at = self.submitted.get(pending.key)
        confirmed = self._quiz_success(pending)
        if not confirmed and submitted_at is not None and now - submitted_at >= 5:
            confirmed = bool(self._result_page_receipts(pending) - pending.receipts_before)
        if confirmed:
            return self._finish_quiz(pending, current)
        if submitted_at is None:
            raise ManualRequired('答案已填写，请检查是否已手动提交这份测验。')
        if now - submitted_at >= 30:
            raise ManualRequired('已尝试提交小测，但尚未看到本次交卷结果。请检查结果或确认弹窗。')
        if not pending.frame.is_detached():
            self._confirm_submission(pending, now)
        return 'working'

    @staticmethod
    def _result_page_receipts(item):
        """同一课件替换整个表单时，读取交卷回执或逐题判分标记。"""
        if item.frame.is_detached():
            return set()
        expected = len(item.root) if isinstance(item.root, (list, tuple)) else 0
        result = item.frame.evaluate(r"""expected => {
            const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden' &&
                !e.closest('nav,aside,header,footer,[aria-hidden=true]');
            const panes = [...document.querySelectorAll('main,[role=main],.RightCon.newTestCon')].filter(visible);
            const roots = panes.filter(e => !panes.some(p => e !== p && e.contains(p)));
            if (roots.length !== 1) return {messages:[], graded:false};
            const root = roots[0];
            const messages = [...root.children].filter(e => visible(e) &&
                e.matches('p,.submission-result,.quiz-result')).map(e => e.innerText.trim());
            const labels = [...root.querySelectorAll('*')].filter(e => visible(e) &&
                [...e.childNodes].some(n => n.nodeType === 3 && /^\s*我的答案\s*[:：]/.test(n.textContent)));
            const rows = new Set();
            for (const label of labels) {
                for (let el=label; el && el!==root; el=el.parentElement) {
                    const summary = el.innerText.trim();
                    if (summary.length > 160 || (summary.match(/我的答案\s*[:：]/g)||[]).length !== 1) break;
                    if (/\d+(?:\.\d+)?\s*分\s*$/.test(summary)) { rows.add(el); break; }
                }
            }
            return {messages, graded:expected > 0 && rows.size === expected && labels.length === expected};
        }""", expected)
        receipts = {msg for msg in result['messages'] if SUCCESS.fullmatch(msg)}
        if result['graded']:
            receipts.add('逐题判分完成')
        return receipts

    def _document(self, item, now):
        if not self.auto_documents:
            raise ManualRequired('当前是文档，请阅读后切到下一节，再选择继续。')
        pending = self.document_pending.get(item.key)
        if pending:
            current = self._document_fingerprint(item)
            if current is None or current == pending['before']:
                pending['seen'] = None
                if now - pending['at'] >= 30:
                    raise ManualRequired('文档已尝试翻页，但内容未确认更新。请检查阅读器或手动翻页后继续；不会重复点击。')
                return 'working'
            if current != pending['seen']:
                pending.update(seen=current, since=now)
                return 'working'
            if now - pending['since'] < 0.35:
                return 'working'
            del self.document_pending[item.key]
            self.document_due[item.key] = now + self.document_seconds
            self._emit('已确认文档翻页，等待阅读停留时间')
            return 'working'
        if item.key not in self.document_due:
            self.document_due[item.key] = now + self.document_seconds
            self.document_steps[item.key] = 0
            self._emit(f'正在浏览文档；每页停留 {self.document_seconds} 秒')
            return 'working'
        if item.key in self.document_end:
            if not self.adapter.task_pending():
                self.completed.add(item.key)
                self._emit('文档已浏览至末页，准备进入下一节')
                return 'done'
            if now - self.document_end[item.key] >= 30:
                raise ManualRequired('文档已浏览至末页，但平台仍显示任务点未完成，请检查阅读要求。')
            return 'working'
        if now < self.document_due[item.key]:
            return 'working'
        self._check()
        if self._document_step(item, now):
            self.document_end[item.key] = now
            if not self.adapter.task_pending():
                self.completed.add(item.key)
                self._emit('文档已浏览至末页，准备进入下一节')
                return 'done'
        else:
            self.document_steps[item.key] += 1
            if self.document_steps[item.key] > 1000:
                raise ManualRequired('文档页面变化过多，请检查阅读器是否正常。')
            self.document_due[item.key] = now + self.document_seconds
            self._emit(f'正在浏览文档：已翻阅 {self.document_steps[item.key]} 页/屏')
        return 'working'

    def _document_fingerprint(self, item):
        data = item.root.evaluate("""e => {
            if (e.matches('[aria-busy=true]') || e.querySelector('[aria-busy=true]')) return null;
            const copy=e.cloneNode(true);
            copy.querySelectorAll('button,script,style,[role=button]').forEach(n=>n.remove());
            return {text:(copy.textContent||'').replace(/\\s+/g,' ').trim(),
                pages:[...e.querySelectorAll('img,.page,[data-page-number]')].map(n=>[
                    n.src||'',n.getAttribute('data-page-number')||'',n.getAttribute('aria-current')||'']),
                pageNumber:e.ownerDocument.querySelector('#pageNumber')?.value||''};
        }""")
        return None if data is None else _digest(str(data))

    def _document_step(self, item, now):
        frame = item.frame
        buttons = frame.get_by_role('button', name=NEXT_PAGE).all()
        buttons += frame.get_by_role('link', name=NEXT_PAGE).all()
        # PDF.js 的箭头控件可能只有 id，限定在已识别的阅读器中。
        buttons += frame.locator('#next, #nextPage').all()
        buttons = [button for button in buttons if button.is_visible()]
        enabled = [button for button in buttons if button.is_enabled()
                   and button.get_attribute('aria-disabled') != 'true'
                   and 'disabled' not in (button.get_attribute('class') or '').lower()]
        if enabled:
            # 同一按钮可能同时匹配 role 与 id；由 DOM 是否相同去重。
            unique = []
            for candidate in enabled:
                if not any(candidate.evaluate('(e, other) => e === other',
                                              other.element_handle()) for other in unique):
                    unique.append(candidate)
            button = only_one(unique, '文档下一页按钮')
            self._check()
            before = self._document_fingerprint(item)
            if before is None:
                raise ManualRequired('文档仍在加载，请等待内容出现后继续。')
            # 点击前登记；异常、超时或页面尚未响应均不能触发第二次点击。
            self.document_pending[item.key] = {'before': before, 'at': now, 'seen': None}
            button.click(timeout=5000)
            return False
        if buttons:
            return True
        if frame.locator('embed:visible, object:visible').count():
            raise ManualRequired('这个文档阅读器未开放可识别的翻页控件，请手动阅读后继续。')
        state = item.root.evaluate('e => {' + SCROLL_ROOT + """
            const s = scrollRoot(e);
            return {top:s.scrollTop, height:s.scrollHeight, viewport:s.clientHeight,
                    text:(e.innerText || '').trim().length,
                    pages:e.querySelectorAll('canvas, img, .page').length};
        }""")
        if state['viewport'] < 10 or (not state['text'] and not state['pages']):
            raise ManualRequired('文档内容尚未加载或无法识别，请检查阅读器。')
        if state['height'] <= state['top'] + state['viewport'] + 3:
            return True
        self._check()
        item.root.evaluate('e => {' + SCROLL_ROOT + """
            const s = scrollRoot(e);
            s.scrollBy(0, Math.max(100, s.clientHeight * 0.85));
        }""")
        return False

    def _finish_quiz(self, item, current):
        self._clear_confirmation()
        self.pending_quiz = None
        self.awaiting_manual_submit.discard(item.key)
        running = [video for video in self.adapter.videos() if not video.state.ended]
        if running:
            self.inline_completed.add(item.key)
            for video in running:
                self.adapter.start_attempts.discard(video.state.key)
            self._emit('小测已提交，继续当前视频')
            return 'working'
        self.completed.add(item.key)
        self.last_activity = item
        if current and current.kind == 'document' and current.frame == item.frame:
            self.completed.add(current.key)
            self.last_activity = current
        elif current and current.kind == 'quiz' and self.screenshot_quiz:
            if self._attempt(current) == self._attempt(item):
                self.completed.add(current.key)
        self._emit('页面已确认小测提交，准备进入下一节')
        self._dismiss_success_prompt(item)
        return 'done'

    def _quiz(self, item, now):
        self.pause_context = 'quiz'
        attempt = self._attempt(item)
        if (item.key in self.submitted or attempt in self.submitted_attempts
                or item.key in self.awaiting_manual_submit):
            if self._quiz_success(item):
                return self._finish_quiz(item, self._detect() if self.screenshot_quiz else None)
            if item.key in self.awaiting_manual_submit:
                raise ManualRequired('答案已填写并核对，请检查后手动提交，再选择继续；不会重复调用 AI。')
            submitted_at = self.submitted.get(item.key)
            if submitted_at is None:
                submitted_at = self.submitted_attempts.get(attempt, now)
            if now - submitted_at >= 30:
                raise ManualRequired('已尝试提交小测，但未看到提交成功。请检查结果或确认弹窗，不会自动重复提交。')
            self._confirm_submission(item, now)
            return 'working'
        if not self.auto_quiz:
            raise ManualRequired('当前是小测，请答题并提交后，切到下一节再选择继续。')
        if self.screenshot_quiz:
            if self._attempt(item) in self.quiz_attempts:
                raise ManualRequired('本任务已尝试交卷，页面内容后来发生变化；请核对结果，不会再次调用 AI 或重复提交。')
        if self.screenshot_quiz and not self.vision_model.strip():
            raise ManualRequired('截图答题需要图片模型，请在“自动学习设置”配置；不会改用本机文字识别。')
        if self.reviewed_quiz is None:
            reviewed = self._request_answers(item)
            if self.screenshot_quiz:
                self.reviewed_quiz = reviewed
                self._emit('已获得整组答案，正在本地核对题目与控件')
        else:
            reviewed = self.reviewed_quiz
            self._emit('沿用本次已核对的 AI 答案，正在本地核对题目')
        questions, answers = reviewed.questions, reviewed.answers
        task_context = item.task_context
        self._check()
        fresh = self._detect()
        # 同一网址换 Task 时题目可能完全相同，key 不能区分，必须比较任务上下文。
        if fresh is None or fresh.kind != 'quiz' or fresh.task_context != task_context:
            self.reviewed_quiz = None
            raise ManualRequired('AI 返回时题目页面已变化，请确认当前页面后继续。')
        # 页面指纹（忽略作答状态与纯样式，只反映题干/选项/材料结构）在请求期间变化时，
        # 说明题目真的变了；这比截图字节比较更稳健，不受滚动、浮层、重绘影响。
        if self.screenshot_quiz and fresh.key != item.key:
            self.reviewed_quiz = None
            raise ManualRequired('AI 返回时题目内容已变化，请确认当前页面后继续。')
        # 请求期间可能重绘控件或增加题目，重新读取完整试卷并使用当前控件。
        try:
            fresh_questions, bindings = self._read_quiz(fresh)
        except ManualRequired as exc:
            if self.reviewed_quiz is None:
                raise
            raise ManualRequired(f'{exc} 本组答案已保留，处理后选择继续即可在本地重新核对。') from exc
        comparable = ([replace(q, images=[], audio=[]) for q in questions] if self.screenshot_quiz else questions)
        fresh_comparable = ([replace(q, images=[], audio=[]) for q in fresh_questions]
                            if self.screenshot_quiz else fresh_questions)
        # AI 请求耗时较长，期间页面滚动/浮层/重绘会改变截图字节；只比较题目内容与页面指纹，
        # 忽略纯视觉差异，避免把截图字节不同误判成“题目内容已变化”而不填写不提交。
        if fresh_comparable != comparable:
            self.reviewed_quiz = None
            raise ManualRequired('AI 返回时题目内容已变化，请确认当前页面后继续。')
        # 截图模式的问题对象只有控件编号，必须另存一份忽略作答状态的页面指纹来发现选项文字变化。
        before_print = self._fingerprint(fresh) if self.screenshot_quiz else None
        self._fill_answers(bindings, answers)
        self._check()
        # 填写可能触发新题目、重绘或同名单选框互相清除；交卷前复核整卷。
        # 作答状态会改变页面指纹（例如超星的 aria-label），所以这里按题目内容判断。
        final_item = self._detect()
        if (final_item is None or final_item.kind != 'quiz'
                or final_item.task_context != task_context):
            raise ManualRequired('填写后题目页面已变化，请检查完整试卷，不会自动提交。')
        if before_print is not None and self._fingerprint(final_item) != before_print:
            raise ManualRequired('填写后题目内容已变化，请检查完整试卷，不会自动提交。')
        final_questions, final_bindings = self._read_quiz(final_item, capture=not self.screenshot_quiz)
        if final_questions != comparable:
            raise ManualRequired('填写后题目内容已变化，请检查完整试卷，不会自动提交。')
        for answer in answers:
            binding = final_bindings[int(answer.id)]
            if isinstance(binding, DragBinding):
                verify_drag(binding, answer.placements)
            elif isinstance(binding, FillBinding):
                verify_fill(binding, answer.filled)
            else:
                question = final_questions[int(answer.id)]
                actual = self._verify_selection(binding, set(answer.selected))
                if question.page_letters:
                    requested = ''.join(sorted(question.options[i] for i in answer.selected))
                    checked = ''.join(sorted(question.options[i] for i in actual))
                    self._emit(f'第 {int(answer.id)+1} 题：AI 选择 {requested}，网页已选 {checked}')
        self._check()
        item = final_item
        self.success_before[item.key] = self._success_signals(item)
        item.receipts_before = frozenset(self._result_page_receipts(item))
        self.success_baseline[self._attempt(item)] = self.success_before[item.key]
        self.reviewed_quiz = None
        if not self.quiz_submit:
            self.awaiting_manual_submit.add(item.key)
            self.pending_quiz = item
            self._emit('答案已填写并核对，等待你检查后手动提交')
            raise ManualRequired('答案已填写并核对，请检查后手动提交，再选择继续；不会重复调用 AI。')
        # 发送前登记，超时不能导致重复交卷。
        self.submitted[item.key] = time.monotonic()
        self.submitted_attempts[self._attempt(item)] = self.submitted[item.key]
        if self.screenshot_quiz:
            self.quiz_attempts.add(self._attempt(item))
        self.pending_quiz = item
        try:
            self._emit('答案已填写并核对，正在滚动到提交按钮')
            self._submit_quiz(item)
        except Exception:
            raise ManualRequired('提交操作未确认完成，请检查当前小测。程序不会自动重复交卷。') from None
        self._emit('已填写答案并提交小测，等待页面确认结果')
        return 'working'

    def _request_answers(self, item):
        self._report('本组尚未收到 AI 答案。')
        questions, _ = self._read_quiz(item)
        model = self.vision_model if self.screenshot_quiz else self.ai_model
        if not all((self.ai_base_url.strip(), model.strip(), self.ai_api_key.strip())):
            raise ManualRequired('自动小测尚未配置 AI 接口。请停止后打开“自动学习设置”填写，或手动答题后继续。')
        blanks = sum(len(question.blanks) for question in questions)
        image_count = len({image.id for q in questions for image in q.images})
        audio_count = len({sound.id for q in questions for sound in q.audio})
        self._emit(f'正在调用 AI 解答小测：{len(questions)} 道题，{blanks} 个填空位置；'
                   f'图片 {image_count}，音频 {audio_count}')
        review = (dict(cross_check=True, review_model=self.ai_review_model,
                       progress=self._emit, check=self._check) if self.quiz_cross_check else {})
        review['report'] = self._report
        if self.screenshot_quiz:
            review.update(progress=self._emit, check=self._check)
        if self.quiz_cross_check and self.web_review is not None:
            review['web_review'] = self.web_review
        try:
            if any(q.images or q.audio for q in questions):
                answers = solve_questions(questions, self.ai_base_url, model, self.ai_api_key,
                    vision_model=self.vision_model, speech_base_url=self.speech_base_url,
                    speech_model=self.speech_model, speech_api_key=self.speech_api_key, **review)
            else:
                answers = solve_questions(questions, self.ai_base_url, self.ai_model, self.ai_api_key, **review)
        except QuizDisagreement:
            raise
        except QuizAIError as exc:
            raise ManualRequired(str(exc)) from None
        return ReviewedQuiz(item.key, questions, answers, self._attempt(item))

    def _read_quiz(self, item, capture=True):
        if self.screenshot_quiz:
            from quiz_screenshots import read_screenshots
            return read_screenshots(item, capture=capture, check=self._check, progress=self._emit)
        return read_quiz(item, multimedia=bool(self.vision_model or self.speech_model),
                         check=self._check, progress=self._emit)

    def _fill_answers(self, bindings, answers):
        for answer in answers:
            self._check()
            options = bindings[int(answer.id)]
            if isinstance(options, DragBinding):
                fill_drag(options, answer.placements, self._check, self.adapter.page)
                continue
            if isinstance(options, FillBinding):
                fill_text(options, answer.filled, self._check)
                continue
            selected = set(answer.selected)
            for index, (option, target) in enumerate(options):
                expected = index in selected
                role = option.get_attribute('role')
                checked = option_checked(option)
                if checked == expected:
                    continue
                if not expected and (role or option.get_attribute('type')) == 'radio':
                    continue
                self._check()
                target.click(timeout=5000)
            # 一题完整填写并验证后才处理下一题。
            self._verify_selection(options, selected)

    def _verify_selection(self, options, selected):
        actual = [index for index, (option, _) in enumerate(options) if option_checked(option)]
        if actual != sorted(selected):
            raise ManualRequired('网页未保留完整选项，请检查答案，不会自动提交。')
        return actual

    def _submit_quiz(self, item):
        buttons = item.frame.get_by_role('button', name=SUBMIT).all()
        buttons += item.frame.get_by_role('link', name=SUBMIT).all()
        buttons = [button for button in buttons if button.is_visible() and button.is_enabled()]
        button = only_one(buttons, '小测提交按钮')
        if button is None:
            raise ManualRequired('答案已填入，但未识别到唯一的提交按钮，请手动提交。')
        self._clear_confirmation()
        self.confirmation_before = [dialog.element_handle() for dialog in self._submission_dialogs(item)]
        # 给点击前已显示的确认层打标记，提交后新出现的层才允许自动点击。
        self.confirmation_frames = self._mark_old_layers(item)
        self.confirmation_key = item.key
        button.click(timeout=5000)

    def _clear_confirmation(self):
        self.confirmation_key = None
        self._clear_old_layers()
        for handle in self.confirmation_before:
            try:
                handle.dispose()
            except Exception:
                pass
        self.confirmation_before = []

    def _confirmation_ready(self, item, now):
        submitted_at = self.submitted.get(item.key)
        if submitted_at is None:
            submitted_at = self.submitted_attempts.get(self._attempt(item))
        return bool(self.quiz_submit and item is not None and
                    self.confirmation_key == item.key and item.key not in self.confirmed and
                    submitted_at is not None and 0 <= now - submitted_at < 30)

    def _on_submission_dialog(self, dialog):
        item = self.pending_quiz
        if (not self._confirmation_ready(item, time.monotonic()) or
                dialog.type != 'confirm' or not _submission_confirmation(dialog.message)):
            return False
        # 原生弹窗打开时不能读取 DOM；只检查取消信号和已登记的交卷意图。
        if self.control is not None:
            self.control.check()
        self.confirmed.add(item.key)
        dialog.accept()
        self._emit('已确认交卷，等待页面显示提交结果')
        return True

    def _submission_dialogs(self, item):
        # 弹窗可能由课件的父页面展示，不检查其他课件或其他标签页。
        dialogs = []
        for frame in _frame_chain(item.frame):
            dialogs.extend(dialog for dialog in frame.locator(SUBMIT_DIALOG).all()
                           if dialog.is_visible())
        return dialogs

    def _confirm_submission(self, item, now):
        if not self._confirmation_ready(item, now):
            return
        try:
            self._confirm_submission_once(item, now)
        except ExactTimeoutError:
            # 提交后页面正在跳转；下一轮再确认，不当作失败。
            return

    def _confirm_submission_once(self, item, now):
        candidates = []
        for dialog in self._submission_dialogs(item):
            if any(dialog.evaluate('(e, before)=>e===before', before)
                   for before in self.confirmation_before):
                continue
            if not _submission_confirmation(dialog.inner_text()):
                continue
            if dialog.locator('input:not([type=button]):not([type=submit]),textarea,select').count():
                continue
            for button in dialog.locator(
                    'button,[role=button],a,input[type=button],input[type=submit],'
                    '[class*=btn],[class*=button],[class*=confirm],[class*=submit]').all():
                label = button.get_attribute('value') if button.evaluate('e=>e.tagName') == 'INPUT' else button.inner_text()
                if (button.is_visible() and button.is_enabled() and
                        button.get_attribute('aria-disabled') != 'true' and CONFIRM_BUTTON.fullmatch(label or '')):
                    if not any(button.evaluate('(e,other)=>e===other', other.element_handle())
                               for other in candidates):
                        candidates.append(button)
        button = only_one(candidates, '交卷确认按钮')
        if button is None:
            # 确认框可能不属于已知前端框架（超星、雨课堂等常用自定义层）。
            self._confirm_unknown_layer(item)
            return
        self._check()
        self.confirmed.add(item.key)
        button.click(timeout=5000)
        self._emit('已确认交卷，等待页面显示提交结果')

    def _mark_layer_button(self, item, request, block, button, mark):
        """在课件及其父页面里找唯一的确认按钮；找到时打上临时标记。"""
        for frame in _frame_chain(item.frame):
            try:
                result = frame.evaluate(CONFIRM_LAYER_JS, dict(
                    request=request, block=block or '', button=button, mark=mark))
            except Exception:
                return None, 0
            count = result.get('count', 0) if isinstance(result, dict) else 0
            if count:
                return frame, count
        return None, 0

    def _mark_old_layers(self, item):
        """给交卷前已显示的确认层打标记；提交后出现的层才允许自动点击。"""
        frames = []
        for frame in _frame_chain(item.frame):
            try:
                frame.evaluate(PROMPT_TEXTS_JS, CONFIRM_REQUEST.pattern)
                frames.append(frame)
            except Exception:
                pass
        return frames

    def _clear_old_layers(self):
        for frame in self.confirmation_frames:
            try:
                frame.evaluate("() => document.querySelectorAll('[data-ch-old]')"
                               ".forEach(e => e.removeAttribute('data-ch-old'))")
            except Exception:
                pass
        self.confirmation_frames = []

    def _confirm_unknown_layer(self, item):
        """只在层内文字明确要求提交、且只有一个确认按钮时点一次。"""
        frame, count = self._mark_layer_button(
            item, CONFIRM_REQUEST.pattern, CONFIRM_BLOCK.pattern, CONFIRM_BUTTON.pattern,
            'data-ch-confirm')
        if frame is None:
            return
        if count != 1:
            self._emit('检测到多个交卷确认按钮，未自动点击；请手动确认一次')
            return
        target = frame.locator('[data-ch-confirm="1"]')
        try:
            self._check()
            # 点击即产生副作用，先登记以防重复。
            self.confirmed.add(item.key)
            target.click(timeout=5000)
            self._emit('已确认交卷，等待页面显示提交结果')
        finally:
            try:
                target.evaluate('(e)=>e.removeAttribute("data-ch-confirm")')
            except Exception:
                pass

    def _dismiss_success_prompt(self, item):
        """成功提示层可能挡住下一节；只在文字明确、按钮唯一时点一次关闭。"""
        frame, count = self._mark_layer_button(
            item, SUCCESS_PROMPT.pattern, '', SUCCESS_DISMISS.pattern, 'data-ch-success')
        if frame is None or count != 1:
            return
        target = frame.locator('[data-ch-success="1"]')
        try:
            target.click(timeout=3000)
        except Exception:
            pass
        finally:
            try:
                target.evaluate('(e)=>e.removeAttribute("data-ch-success")')
            except Exception:
                pass

    def _success_signals(self, item):
        # 捕获当前测验的作用域；题目移除后仍可看原容器的结果，容器被替换则等待人工核对。
        try:
            if item.success_scope is None:
                scope = quiz_result_scope(item)
                section = item.root[0].locator('xpath=ancestor::section[1]')
                if section.count() == 1 and all(section.evaluate('(e,c)=>e.contains(c)', group.element_handle())
                                                for group in item.root):
                    scope = section
                item.success_scope = scope.element_handle()
            candidates = item.success_scope.evaluate("""root => {
                if (!root.isConnected) return [];
                return [root, ...root.querySelectorAll('*')].filter(e => {
                    if (!e.getClientRects().length || getComputedStyle(e).visibility === 'hidden' ||
                        e.closest('aside,nav,header,footer')) return false;
                    const section = e.closest('section');
                    if (section && section !== root && !section.contains(root)) return false;
                    return true;
                }).map(e => {
                    const status = e.closest('p,output,[role=status],[role=alert],.quiz-result,.submission-result');
                    // 只接受本节点直接拥有的文字，祖先不能聚合已排除旁栏/其他题组的成功消息。
                    const ownText = Array.from(e.childNodes).filter(n => n.nodeType === Node.TEXT_NODE)
                        .map(n => n.textContent).join('').trim();
                    return {text:ownText,
                        surrounding:((status || e.parentElement || e).innerText || '').trim()};
                }).filter(e => e.text.length && e.text.length < 200);
            }""")
            return {candidate['text'] for candidate in candidates if SUCCESS.fullmatch(candidate['text'])
                    and not SUBMIT_FAILURE.search(candidate['surrounding'])}
        except Exception:
            return set()

    def _quiz_success(self, item):
        baseline = self.success_before.get(item.key)
        if baseline is None:
            baseline = self.success_baseline.get(self._attempt(item))
        return bool(self._success_signals(item) - (baseline or set()))
