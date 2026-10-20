"""图形界面的工作线程；不从工作线程访问 Tk 控件。"""
from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import threading

from browser import Adapter, install_background_playback
from core import Store, validate_url
from main import START, Task, run_task


class Cancelled(KeyboardInterrupt):
    """通过独立异常退出交互和等待，仍执行资源清理。"""


@dataclass
class GuiRequest:
    command: str
    platform: str
    task_id: str
    url: str = ''
    max_chapters: int = 0
    resume: bool = True
    channel: str = 'msedge'
    auto_documents: bool = True
    auto_quiz: bool = True
    document_seconds: int = 10
    ai_base_url: str = ''
    ai_model: str = ''
    ai_api_key: str = field(default='', repr=False)
    quiz_submit: bool = True
    vision_model: str = ''
    speech_base_url: str = ''
    speech_model: str = ''
    speech_api_key: str = field(default='', repr=False)
    background_playback: bool = True


class Bridge:
    def __init__(self, events=None):
        self.events = events if events is not None else queue.Queue()
        self._stopped = threading.Event()
        self._condition = threading.Condition()
        self._serial = 0
        self._pending = None
        self._answer = None
        self._allowed = set()

    def emit(self, kind, **payload):
        self.events.put({'kind': kind, **payload})

    def check(self):
        if self._stopped.is_set():
            raise Cancelled()

    @property
    def stopped(self):
        return self._stopped.is_set()

    def sleep(self, seconds):
        if self._stopped.wait(seconds):
            raise Cancelled()

    def stop(self):
        self._stopped.set()
        with self._condition:
            self._condition.notify_all()

    def ask(self, title, message, choices):
        with self._condition:
            self.check()
            self._serial += 1
            request_id = self._serial
            self._pending = request_id
            self._answer = None
            self._allowed = {key for key, _ in choices}
            self.emit('prompt', request_id=request_id, title=title,
                      message=message, choices=choices)
            try:
                while self._answer is None:
                    self.check()
                    self._condition.wait(timeout=0.1)
                self.check()
                return self._answer
            finally:
                self._pending = None
                self._allowed = set()

    def reply(self, request_id, answer):
        with self._condition:
            if (request_id != self._pending or answer not in self._allowed
                    or self._answer is not None or self._stopped.is_set()):
                return False
            self._answer = answer
            self._condition.notify_all()
            return True


class EventLogHandler(logging.Handler):
    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge

    def emit(self, record):
        self.bridge.emit('log', message=self.format(record))


_task_lock = threading.Lock()
_active_tasks = set()


def task_directory(data_dir, request):
    """同课程共享稳定的本地目录，不把课程名称直接用作文件路径。"""
    identity = json.dumps([request.platform, request.task_id.strip()],
                          ensure_ascii=False, separators=(',', ':'))
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()
    return Path(data_dir) / 'tasks' / digest


def _claim_task(data_dir, request):
    key = (os.path.normcase(str(Path(data_dir).resolve())),
           request.platform, request.task_id.strip())
    with _task_lock:
        if key in _active_tasks:
            raise ValueError('这门课程已在运行，请先停止该任务，再重新启动或检测页面。')
        _active_tasks.add(key)
    return key


class _TaskLogRouter(logging.Handler):
    """固定挂在 root 上，按工作线程路由到该课程的日志处理器。"""

    def __init__(self, route_lock):
        super().__init__()
        self.route_lock = route_lock
        self.routes = {}

    def emit(self, record):
        # 派发与移除路由共用锁，防止取出处理器后它已被另一个线程关闭。
        with self.route_lock:
            for handler in self.routes.get(record.thread, ()):
                handler.handle(record)


class _TaskLogging:
    """首个任务安装路由并调整级别，最后一个任务恢复 root 日志状态。

    其他任务仍存活时只修改路由映射，不修改正在被 logging 遍历的
    root.handlers，避免某门课结束时跳过其他课程的日志处理器。
    """

    _lock = threading.Lock()
    _users = 0
    _previous_level = None
    _router = None

    def __init__(self, bridge, path):
        self.root = logging.getLogger()
        self.closed = False
        self.handlers = [logging.FileHandler(path, encoding='utf-8'),
                         EventLogHandler(bridge)]
        self.owner = threading.get_ident()
        for handler in self.handlers:
            handler.setFormatter(logging.Formatter('%(asctime)s %(message)s', '%H:%M:%S'))
        with self._lock:
            if _TaskLogging._users == 0:
                _TaskLogging._previous_level = self.root.level
                _TaskLogging._router = _TaskLogRouter(self._lock)
                self.root.setLevel(min(self.root.level, logging.INFO))
                self.root.addHandler(_TaskLogging._router)
            _TaskLogging._router.routes[self.owner] = self.handlers
            _TaskLogging._users += 1

    def close(self):
        close_error = None
        with self._lock:
            if self.closed:
                return
            self.closed = True
            _TaskLogging._router.routes.pop(self.owner, None)
            for handler in self.handlers:
                try:
                    handler.close()
                except Exception as exc:
                    # 某个日志文件刷新失败，也继续关闭其余处理器并恢复全局状态。
                    close_error = close_error or exc
            _TaskLogging._users -= 1
            if _TaskLogging._users == 0:
                self.root.removeHandler(_TaskLogging._router)
                _TaskLogging._router.close()
                _TaskLogging._router = None
                self.root.setLevel(_TaskLogging._previous_level)
                _TaskLogging._previous_level = None
        if close_error is not None:
            raise close_error


def resolve_target(request, store):
    # 新输入的网址优先；留空时才恢复同名课程的最后一个章节。
    target = request.url.strip()
    if not target and request.resume:
        target = store.resume_url(request.task_id) or ''
    target = target or START[request.platform]
    if not target:
        raise ValueError('通用课程首次使用请填写 HTTPS 课程链接；已有记录可留空恢复。')
    validate_url(target, request.platform)
    return target


def choose_page(context, platform, bridge):
    while True:
        bridge.check()
        pages = []
        for page in context.pages:
            if page.is_closed():
                continue
            try:
                validate_url(page.url, platform)
                title = page.title() or '未命名页面'
            except Exception:
                continue
            pages.append((page, title[:100]))
        if len(pages) == 1:
            return pages[0][0]
        if not pages:
            answer = bridge.ask('未找到课程页面',
                                '请在助手打开的浏览器里登录所选平台，并打开具体课程章节。',
                                [('retry', '重新查找'), ('cancel', '停止')])
            if answer == 'cancel':
                raise Cancelled()
            continue
        answer = bridge.ask('选择课程标签页', '请选择已经打开具体课件的页面：',
                            [(str(i), title) for i, (_, title) in enumerate(pages)]
                            + [('cancel', '停止')])
        if answer == 'cancel':
            raise Cancelled()
        return pages[int(answer)][0]


def execute(request, bridge, data_dir):
    outcome = 'error'
    error_message = None
    task_lease = None
    task_logging = None
    store = None
    try:
        bridge.check()
        if request.command not in {'inspect', 'run'} or request.platform not in START:
            raise ValueError('请选择有效的平台和操作。')
        if not request.task_id.strip():
            raise ValueError('请填写课程名称。')
        if type(request.max_chapters) is not int or not 0 <= request.max_chapters <= 1000:
            raise ValueError('本次视频数须为 0 到 1000 的整数，0 表示不限。')
        if any(type(value) is not bool for value in
               (request.auto_documents, request.auto_quiz, request.quiz_submit, request.background_playback)):
            raise ValueError('文档、小测、自动提交和后台播放设置必须为启用或停用。')
        if type(request.document_seconds) is not int or not 3 <= request.document_seconds <= 120:
            raise ValueError('文档每页停留时间须为 3 到 120 秒的整数。')
        if not all(isinstance(value, str) for value in
                   (request.ai_base_url, request.ai_model, request.ai_api_key,
                    request.vision_model, request.speech_base_url, request.speech_model, request.speech_api_key)):
            raise ValueError('AI 接口地址、模型和 API Key 必须为文本。')
        if request.channel not in {'msedge', 'chrome'}:
            raise ValueError('请选择 Edge 或 Chrome。')
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        task_lease = _claim_task(data_dir, request)
        local_dir = task_directory(data_dir, request)
        local_dir.mkdir(parents=True, exist_ok=True)
        store = Store(data_dir / 'progress.sqlite3')
        target = resolve_target(request, store)
        task_logging = _TaskLogging(bridge, local_dir / 'helper.log')
        from playwright.sync_api import sync_playwright

        bridge.emit('status', message='正在打开浏览器…')
        with sync_playwright() as playwright:
            bridge.check()
            try:
                context = playwright.chromium.launch_persistent_context(
                    str(local_dir / f'profile-{request.channel}'), headless=False,
                    viewport=None, channel=request.channel, timeout=20000)
            except Exception:
                raise RuntimeError('浏览器启动失败：请确认已安装所选浏览器，且另一个网课助手窗口没有运行同一课程。') from None
            try:
                bridge.check()
                background_ready = False
                if request.command == 'run':
                    background_ready = install_background_playback(context, request.background_playback)
                page = context.new_page()
                try:
                    page.goto(target, wait_until='domcontentloaded', timeout=20000)
                except Exception:
                    bridge.check()
                    bridge.emit('log', message='页面尚未加载完成，请在浏览器中检查网络或手动打开课程。')
                bridge.check()
                answer = bridge.ask('打开课程后，再继续',
                                    '请在刚打开的浏览器中手动登录，进入要学习的具体课程章节。'
                                    '\n准备好后点击下面的“我已打开课程”。'
                                    + ('\n后台播放兼容已启用：开始后可切换软件或最小化浏览器，不能关闭浏览器。'
                                       if background_ready else
                                       '\n后台播放兼容未完全启用，请查看运行记录并按提示处理。'
                                       if request.background_playback and request.command == 'run' else ''),
                                    [('ready', '我已打开课程'), ('locate', '先定位目录'), ('cancel', '停止')])
                if answer == 'cancel':
                    raise Cancelled()
                page = choose_page(context, request.platform, bridge)
                bridge.emit('status', message='正在检测课程页面…')
                adapter = Adapter(page, request.platform, control=bridge)
                adapter.navigator.load(local_dir / 'navigation.json')
                if answer == 'locate':
                    adapter.navigator.configure(bridge)
                if request.command == 'inspect':
                    report = adapter.inspect()
                    (local_dir / 'inspect.json').write_text(
                        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                    bridge.emit('inspection', report=report)
                    bridge.ask('检测完成', '检测结果已显示在软件的运行记录中。',
                               [('close', '关闭浏览器，返回软件')])
                    outcome = 'inspected'
                else:
                    from activities import CourseActivities
                    bridge.emit('status', message='正在学习，进度会自动保存')
                    task = Task(request.task_id, request.platform, page.url,
                                request.max_chapters)
                    activities = CourseActivities(
                        adapter, auto_documents=request.auto_documents,
                        auto_quiz=request.auto_quiz, document_seconds=request.document_seconds,
                        quiz_submit=request.quiz_submit,
                        ai_base_url=request.ai_base_url, ai_model=request.ai_model,
                        ai_api_key=request.ai_api_key, control=bridge, vision_model=request.vision_model,
                        speech_base_url=request.speech_base_url, speech_model=request.speech_model,
                        speech_api_key=request.speech_api_key)
                    outcome = run_task(adapter, task, store, 2, 90,
                                       control=bridge, activities=activities)
                    if outcome in {'limit', 's'}:
                        bridge.ask('本次学习已结束',
                                   '浏览器会保留，便于你核对课程页面和平台记录。'
                                   '\n确认后可关闭浏览器并返回软件。',
                                   [('close', '关闭浏览器并返回')])
                bridge.check()
            finally:
                context.close()
    except Cancelled:
        outcome = 'cancelled'
    except (ValueError, RuntimeError) as exc:
        error_message = str(exc)
    except ImportError:
        error_message = '运行文件不完整，请重新解压软件包后再试。'
    except Exception as exc:
        error_message = f'运行已停止（{type(exc).__name__}）。请检查网络或重新登录后再试。'
    finally:
        try:
            if store is not None:
                store.close()
        except Exception:
            error_message = error_message or '关闭本地进度记录时发生异常，请重新启动软件。'
        finally:
            try:
                if task_logging is not None:
                    task_logging.close()
            except Exception:
                error_message = error_message or '关闭本地日志时发生异常，请重新启动软件。'
            finally:
                if task_lease is not None:
                    with _task_lock:
                        _active_tasks.discard(task_lease)
        if error_message:
            bridge.emit('log', message=error_message)
            bridge.emit('done', outcome='error', message=error_message)
        else:
            bridge.emit('done', outcome=outcome)
