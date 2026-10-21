"""通过可见阅读器和题目控件处理文档、小测；不修改平台的完成状态。"""
from dataclasses import dataclass
import hashlib
import logging
import re
import time
from urllib.parse import urlsplit

from browser import ManualRequired, only_one, page_identity
from quiz_ai import QuizAIError, solve_questions
from quiz_drag import DragBinding, drag_prompt, is_drag_group, fill_drag, verify_drag
from quiz_dom import (FillBinding, discover_question_groups, task_identity, fill_text, prompt_text,
                      read_quiz, verify_fill, option_text as _option_text,
                      visible_options as _visible_options, quiz_result_scope, option_checked)
from dom_read import retry_dom_read


DOC_SELECTOR = '#viewerContainer, .pdfViewer, [role=document], .document-viewer, .reader-container'
DOC_PATH = re.compile(r'/(?:pdf|ppt|document|read|doc)(?:/|\.)', re.I)
NEXT_PAGE = re.compile(r'^\s*(?:下一页|下页|Next\s+page)\s*$', re.I)
SUBMIT = re.compile(r'^\s*(?:提\s*交(?:\s*答案|\s*作业)?|交\s*卷|Submit(?:\s+(?:answers|quiz|test))?)\s*$', re.I)
SUCCESS = re.compile(r'^\s*(?:(?:(?:本次|当前)?(?:答案|作业|小测|测验|试卷))?(?:提交成功|已提交)|'
    r'(?:作业|测验|小测)已完成|(?:本次得分|最终成绩)\s*[:：]\s*\d+(?:\.\d+)?(?:\s*/\s*\d+)?\s*分?|'
    r'(?:(?:your\s+)?(?:answers?|quiz|test)\s+)?submitted successfully|submission successful|'
    r'(?:quiz|test) completed|final score\s*:\s*\d+(?:\.\d+)?(?:\s*/\s*\d+)?)\s*[.!。！]?\s*$', re.I)
SUBMIT_FAILURE = re.compile(r'\b(?:not|never)\s+(?:been\s+)?(?:submitted|completed|successful)|'
    r'\b(?:failed|unsuccessful|failure|try again)\b|未(?:能|曾)?提交|未完成|未成功|失败|重试', re.I)

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


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:32]


class CourseActivities:
    def __init__(self, adapter, auto_documents=True, auto_quiz=True, document_seconds=10,
                 ai_base_url='', ai_model='', ai_api_key='', control=None, quiz_submit=True,
                 vision_model='', speech_base_url='', speech_model='', speech_api_key=''):
        self.adapter = adapter
        self.auto_documents = auto_documents
        self.auto_quiz = auto_quiz
        self.document_seconds = document_seconds
        self.ai_base_url = ai_base_url
        self.ai_model = ai_model
        self.ai_api_key = ai_api_key
        self.vision_model = vision_model
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
        self.completed = set()
        self.inline_completed = set()
        self.last_activity = None
        self.pending_quiz = None
        self.success_before = {}
        # Adapter 用同一识别器判断“下一节”已转到文档/小测。
        self.adapter.activity_identity = self.identity
        self.adapter.course_handled = self._course_handled

    def _course_handled(self):
        item = self._detect()
        return bool(item and item.key in self.inline_completed)

    def _emit(self, message):
        logging.info('%s', message)
        if self.control is not None:
            self.control.emit('status', message=message)

    def _check(self):
        if self.control is not None:
            self.control.check()
        reason = self.adapter.guard(include_course=False)
        if reason:
            raise ManualRequired(reason)

    def _groups(self, frame):
        return discover_question_groups(frame)

    def _page_context(self):
        identity = getattr(self.adapter, 'content_identity', None)
        return identity() if callable(identity) else page_identity(self.adapter.page.url)

    def _static_roots(self, frame):
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
            return item.key
        previous = self.last_activity
        if (previous and previous.key in self.completed and previous.context
                and previous.context.startswith(self._page_context() + '|')
                and not any(not video.state.ended for video in self.adapter.videos())):
            return previous.key
        return ''

    def process(self):
        self._check()
        navigator = getattr(self.adapter, 'navigator', None)
        if navigator is not None:
            navigator.ensure_ready()
        now = time.monotonic()
        if self.pending_quiz is not None:
            pending = self.pending_quiz
            changed_task = (pending.task_context is not None and
                            task_identity(pending.frame) != pending.task_context)
            if changed_task or (pending.context and not pending.context.startswith(
                    self._page_context() + '|')):
                self.pending_quiz = None
                self.completed.add(pending.key)
                raise ManualRequired('小测页面或 Task 已切换，请核对上一任务结果，再选择继续；不会重复提交。')
            self.last_activity = pending
            return self._quiz(pending, now)
        item = self._detect()
        self.last_activity = item
        if item is None:
            return None
        if item.key in self.inline_completed:
            return None
        if item.key in self.completed:
            return 'done'
        if item.kind == 'document':
            return self._document(item, now)
        return self._quiz(item, now)

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

    def _quiz(self, item, now):
        if item.key in self.submitted or item.key in self.awaiting_manual_submit:
            if self._quiz_success(item):
                self.pending_quiz = None
                self.awaiting_manual_submit.discard(item.key)
                videos = self.adapter.videos()
                running = [video for video in videos if not video.state.ended]
                if running:
                    self.inline_completed.add(item.key)
                    for video in running:
                        self.adapter.start_attempts.discard(video.state.key)
                    self._emit('小测已提交，继续当前视频')
                    return 'working'
                self.completed.add(item.key)
                self._emit('页面已确认小测提交，准备进入下一节')
                return 'done'
            if item.key in self.awaiting_manual_submit:
                raise ManualRequired('答案已填写并核对，请检查后手动提交，再选择继续；不会重复调用 AI。')
            if now - self.submitted[item.key] >= 30:
                raise ManualRequired('已尝试提交小测，但未看到提交成功。请检查结果或确认弹窗，不会自动重复提交。')
            return 'working'
        if not self.auto_quiz:
            raise ManualRequired('当前是小测，请答题并提交后，切到下一节再选择继续。')
        questions, bindings = self._read_quiz(item)
        if not all((self.ai_base_url.strip(), self.ai_model.strip(), self.ai_api_key.strip())):
            raise ManualRequired('自动小测尚未配置 AI 接口。请停止后打开“自动学习设置”填写，或手动答题后继续。')
        blanks = sum(len(question.blanks) for question in questions)
        self._emit(f'正在调用 AI 解答小测：{len(questions)} 道题，{blanks} 个填空位置；'
                   f'图片 {sum(len(q.images) for q in questions)}，音频 {sum(len(q.audio) for q in questions)}')
        try:
            if any(q.images or q.audio for q in questions):
                answers = solve_questions(questions, self.ai_base_url, self.ai_model, self.ai_api_key,
                    vision_model=self.vision_model, speech_base_url=self.speech_base_url,
                    speech_model=self.speech_model, speech_api_key=self.speech_api_key)
            else:
                answers = solve_questions(questions, self.ai_base_url, self.ai_model, self.ai_api_key)
        except QuizAIError as exc:
            raise ManualRequired(str(exc)) from None
        self._check()
        fresh = self._detect()
        if fresh is None or fresh.key != item.key:
            raise ManualRequired('AI 返回时题目页面已变化，请确认当前页面后继续。')
        # 请求期间可能重绘控件或增加题目，重新读取完整试卷并使用当前控件。
        fresh_questions, bindings = self._read_quiz(fresh)
        if fresh_questions != questions:
            raise ManualRequired('AI 返回时题目内容已变化，请确认当前页面后继续。')
        self._fill_answers(bindings, answers)
        self._check()
        # 填写可能触发新题目、重绘或同名单选框互相清除；交卷前复核整卷。
        final_item = self._detect()
        if final_item is None or final_item.key != item.key:
            raise ManualRequired('填写后题目页面已变化，请检查完整试卷，不会自动提交。')
        final_questions, final_bindings = self._read_quiz(final_item)
        if final_questions != questions:
            raise ManualRequired('填写后题目内容已变化，请检查完整试卷，不会自动提交。')
        for answer in answers:
            binding = final_bindings[int(answer.id)]
            if isinstance(binding, DragBinding):
                verify_drag(binding, answer.placements)
            elif isinstance(binding, FillBinding):
                verify_fill(binding, answer.filled)
            else:
                self._verify_selection(binding, set(answer.selected))
        self._check()
        self.success_before[item.key] = self._success_signals(item)
        if not self.quiz_submit:
            self.awaiting_manual_submit.add(item.key)
            self.pending_quiz = item
            self._emit('答案已填写并核对，等待你检查后手动提交')
            raise ManualRequired('答案已填写并核对，请检查后手动提交，再选择继续；不会重复调用 AI。')
        # 发送前登记，超时不能导致重复交卷。
        self.submitted[item.key] = time.monotonic()
        self.pending_quiz = item
        try:
            self._submit_quiz(item)
        except Exception:
            raise ManualRequired('提交操作未确认完成，请检查当前小测。程序不会自动重复交卷。') from None
        self._emit('已填写答案并提交小测，等待页面确认结果')
        return 'working'

    def _read_quiz(self, item):
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

    def _submit_quiz(self, item):
        buttons = item.frame.get_by_role('button', name=SUBMIT).all()
        buttons += item.frame.get_by_role('link', name=SUBMIT).all()
        buttons = [button for button in buttons if button.is_visible() and button.is_enabled()]
        button = only_one(buttons, '小测提交按钮')
        if button is None:
            raise ManualRequired('答案已填入，但未识别到唯一的提交按钮，请手动提交。')
        button.click(timeout=5000)

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
        return bool(self._success_signals(item) - self.success_before.get(item.key, set()))
