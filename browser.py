"""页面适配：只操作可见播放器和明确的下一节控件。"""
from dataclasses import dataclass
from functools import wraps
import hashlib
import logging
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from core import VideoState, validate_url, detect_platform
from navigation import Navigator, NavigationTarget
from dom_read import is_loading_error, retry_dom_read


class ManualRequired(RuntimeError):
    pass


BACKGROUND_JS = r"""(() => {
  const installed = Symbol.for('courseHelper.backgroundPlayback');
  if (window[installed]) return true;
  Object.defineProperty(window, installed, {value: true});
  // 超星等平台用定时轮询 document.hasFocus() 判断失焦并暂停视频；事件拦截管不住轮询。
  // 只在页面（含嵌套 iframe）存在视频时伪装焦点，登录、验证码等页面保持真实焦点语义。
  const realHasFocus = Document.prototype.hasFocus;
  const hasMedia = (doc, depth) => {
    try {
      if (!doc || typeof doc.querySelector !== 'function') return false;
      if (doc.querySelector('video')) return true;
      if (depth >= 8) return false;
      for (const frame of doc.querySelectorAll('iframe')) {
        try {
          if (frame.contentDocument && hasMedia(frame.contentDocument, depth + 1)) return true;
        } catch (_) {}
      }
    } catch (_) {}
    return false;
  };
  try {
    Document.prototype.hasFocus = function () {
      try {
        if (hasMedia(this, 0)) return true;
      } catch (_) {}
      return realHasFocus.call(this);
    };
  } catch (_) {}
  // Playwright 已保持页面活跃；补充事件处理，不改播放时间、倍速或进度请求。
  const stop = event => event.stopImmediatePropagation();
  window.addEventListener('blur', event => {
    // 输入框间切换产生的 blur 必须交给表单处理。
    if (event.target === window || event.target === document) stop(event);
  }, true);
  for (const name of ['visibilitychange', 'webkitvisibilitychange']) {
    window.addEventListener(name, event => {
      if (document.hidden || document.webkitHidden || document.visibilityState === 'hidden') stop(event);
    }, true);
  }
  const playerSelector = '.video-js,.xgplayer,.dplayer,.prism-player,.jwplayer,' +
    '.plyr,.mejs-container,.html5-video-player,[data-player]';
  const leavingPlayer = event => {
    if (!(event.target instanceof Element)) return;
    for (const video of document.querySelectorAll('video')) {
      if (!video.getClientRects().length) continue;
      let player = video.closest(playerSelector) || video;
      // 未知布局只接管视频，普通父容器可能同时包含目录或表单。
      if (!player || ['BODY','HTML','MAIN'].includes(player.tagName)) player = video;
      if (event.target !== player && !player.contains(event.target)) continue;
      if (event.relatedTarget instanceof Node && player.contains(event.relatedTarget)) continue;
      stop(event);
      return;
    }
  };
  for (const name of ['mouseleave', 'mouseout', 'pointerleave', 'pointerout']) {
    window.addEventListener(name, leavingPlayer, true);
  }
  // 超星 studyControl 在 top 的冒泡阶段监听 mouseout，而视频位于两层 iframe 内。
  // 只处理真正移出课程窗口的事件；元素自身的收起菜单等处理仍先执行。
  const isChaoxingCourse = window === window.top &&
    (location.hostname === 'chaoxing.com' || location.hostname.endsWith('.chaoxing.com')) &&
    /^\/(?:mooc-ans\/)?mycourse\/studentstudy\/?$/.test(location.pathname);
  if (isChaoxingCourse) {
    const visible = element => {
      const style = element.ownerDocument.defaultView.getComputedStyle(element);
      return Array.from(element.getClientRects()).some(rect => rect.width > 0 && rect.height > 0) &&
        style.visibility !== 'hidden' && style.visibility !== 'collapse';
    };
    const hasVisibleVideo = (doc, depth = 0) => {
      if (Array.from(doc.querySelectorAll('video')).some(visible)) return true;
      if (depth >= 8) return false;
      for (const frame of doc.querySelectorAll('iframe')) {
        if (!visible(frame)) continue;
        try {
          if (frame.contentDocument && hasVisibleVideo(frame.contentDocument, depth + 1)) return true;
        } catch (_) {
          // 跨域课件交给各自的播放器兼容逻辑，不推测其内容。
        }
      }
      return false;
    };
    window.addEventListener('mouseout', event => {
      if (event.relatedTarget || event.toElement) return;
      if (hasVisibleVideo(document)) stop(event);
    });
  }
  return true;
})()"""


def install_background_playback(context, enabled=True):
    """在课件脚本前注册兼容处理；只作用于助手创建的浏览器。"""
    if not enabled:
        return False
    try:
        context.add_init_script(script=BACKGROUND_JS)
    except Exception:
        logging.warning('后台播放兼容未能启用，继续按普通模式运行；请检查浏览器版本。')
        return False
    complete = True
    needs_reload = False
    try:
        for page in context.pages:
            if page.url not in ('', 'about:blank'):
                needs_reload = True
            for frame in page.frames:
                try:
                    handle = frame.wait_for_function(BACKGROUND_JS, timeout=1500)
                    handle.dispose()
                except Exception:
                    complete = False
    except Exception:
        complete = False
    if needs_reload:
        # 已经注册的 window 捕获监听器先执行；补装脚本不能撤销它们。
        logging.warning('后台播放兼容已用于新页面；已打开的课程页请刷新后再开始，以便完整应用。')
    elif complete:
        logging.info('后台播放兼容已启用：可切换到其他软件或最小化课程窗口；请勿关闭课程浏览器。')
    else:
        logging.warning('后台播放兼容已用于新页面；部分已打开页面未能应用，请重新打开课程页后继续。')
    return complete and not needs_reload


def page_identity(url):
    """保留章节标识，忽略会变化的签名；MOOC 的 hash 路由也属于章节。"""
    parts = urlsplit(url)
    volatile = {'enc', 'openc', 'token', 'sign', 'signature', 'expires', 't', '_', 'v'}
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                   if k.lower() not in volatile)
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path,
                       urlencode(query), parts.fragment))


def only_one(items, label):
    if len(items) > 1:
        raise ManualRequired(f'找到多个{label}，请先关闭其他播放器或配置精确选择器。')
    return items[0] if items else None


@dataclass
class Video:
    locator: object
    frame: object
    state: VideoState
    slot: str = ''


VIDEO_JS = """v => ({
  source: v.currentSrc || v.src || '', id: v.id,
  current: v.currentTime, duration: Number.isNaN(v.duration) ? 0 : v.duration,
  paused: v.paused, ended: v.ended, ready: v.readyState,
  error: v.error ? v.error.code : 0, playback_rate: v.playbackRate
})"""

PLAYBACK_RATE_JS = """(v, config) => {
  const name = '__courseHelperPlaybackRates1612';
  const states = window[name] || (window[name] = new WeakMap());
  const previous = states.get(v);
  const key = config.key + '|' + (v.currentSrc || v.src || '');
  let attempted = false, rejected = false;
  if (!previous || previous.key !== key || previous.rate !== config.rate) {
    states.set(v, {key, rate: config.rate});
    attempted = true;
    try { if (v.playbackRate !== config.rate) v.playbackRate = config.rate; }
    catch (_) { rejected = true; }
  }
  return {attempted, rejected, actual: v.playbackRate};
}"""

# 播放器常用自己的覆盖层元素做播放按钮，而不是可访问的 button。
PLAY_BUTTON_SELECTORS = (
    '.vjs-big-play-button',
    '.prism-big-play-btn',
    '.ans-video-player__play-btn',
    '[class*="big-play"]',
    '[class*="play-btn"]',
    '[class*="playBtn"]',
    '[class*="play_btn"]',
    '[aria-label="播放"]',
    '[title="播放"]',
)

ACTIVITY_TABS_JS = r'''() => {
  const visible = e => {
    if (!e.getClientRects().length) return false;
    const s = getComputedStyle(e);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  // 超星等平台把同一节的多个活动做成“1 视频 / 2 章节测验”胶囊标签。
  const KEY = /^(?:\d+\s*[.、)]?\s*)?(?:视频|章节测验|测验|小测|文档|阅读|讨论|作业|音频|课件)$/;
  const textOf = e => (e.innerText || '').trim().replace(/\s+/g, ' ');
  const activeOf = e => e.getAttribute('aria-selected') === 'true' ||
    [...e.classList].some(c => /(^|[-_])(active|current|cur|selected)($|[-_])/i.test(c));
  // 同一逻辑项可能同时匹配 li 与其中的 a/span；统一取最外层同名节点，避免重复计数。
  const activeItem = item => activeOf(item) ||
    [...item.querySelectorAll('[aria-selected],[class]')].some(activeOf);
  for (const old of document.querySelectorAll('[data-ch-tab]')) old.removeAttribute('data-ch-tab');
  const groups = new Map();
  for (const leaf of document.querySelectorAll('a,button,span,li,[role="tab"]')) {
    if (!visible(leaf)) continue;
    const text = textOf(leaf);
    if (!text || text.length > 12 || !KEY.test(text)) continue;
    let item = leaf;
    for (let p = leaf.parentElement; p && p !== document.body && textOf(p) === text; p = p.parentElement) {
      if (p.matches('a,button,li,[role="tab"]')) item = p;
    }
    const countIn = p => p ? [...p.children].filter(c => KEY.test(textOf(c))).length : 0;
    let parent = item.parentElement;
    for (let i = 0; i < 2 && parent && parent !== document.body && countIn(parent) < 2; i++) {
      parent = parent.parentElement;
    }
    if (!parent || parent === document.body || countIn(parent) < 2) continue;
    if (!groups.has(parent)) groups.set(parent, []);
    const list = groups.get(parent);
    if (!list.some(node => node === item)) list.push(item);
  }
  const orderOf = e => {
    const match = textOf(e).match(/^\s*(\d{1,2})/);
    return match ? parseInt(match[1], 10) : 999;
  };
  const found = [];
  for (const items of groups.values()) {
    if (items.length < 2 || items.length > 8) continue;
    // DOM 顺序可能被 CSS 调整过；有“1 视频 / 2 章节测验”这类数字前缀时按数字排。
    const pairs = items.map(item => ({item, flag: activeItem(item), order: orderOf(item)}));
    pairs.sort((a, b) => a.order - b.order);
    const ordered = pairs.map(pair => pair.item);
    const flags = pairs.map(pair => pair.flag);
    if (!flags.includes(true)) continue;
    found.push({items: ordered, flags});
  }
  if (found.length > 1) return {count: found.length, ambiguous: true};
  if (!found.length) return {count: 0};
  const {items, flags} = found[0];
  const index = flags.indexOf(true);
  if (index < 0 || index + 1 >= items.length) return {count: 0};
  items[index + 1].setAttribute('data-ch-tab', '1');
  return {count: 1, text: textOf(items[index + 1])};
}'''


TAB_ACTIVE_JS = r'''(text) => {
  const visible = e => {
    if (!e.getClientRects().length) return false;
    const s = getComputedStyle(e);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const activeOf = e => e.getAttribute('aria-selected') === 'true' ||
    [...e.classList].some(c => /(^|[-_])(active|current|cur|selected)($|[-_])/i.test(c));
  const activeItem = item => activeOf(item) ||
    [...item.querySelectorAll('[aria-selected],[class]')].some(activeOf);
  const norm = e => (e.innerText || '').trim().replace(/\s+/g, ' ');
  return [...document.querySelectorAll('a,button,span,li,[role="tab"]')]
    .filter(e => visible(e) && norm(e) === text)
    .some(activeItem);
}'''


TAB_PROMPT_JS = r'''() => {
  const visible = e => {
    if (!e.getClientRects().length) return false;
    const s = getComputedStyle(e);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const KEY = /^\s*(?:开始答题|开始测验|开始考试|进入测验|继续答题|开始|进入)\s*$/;
  return [...document.querySelectorAll('button,a,[role="button"]')]
    .some(e => visible(e) && KEY.test((e.innerText || '').trim()));
}'''


NEXT_RE = re.compile(r'^\s*(下一节|下一章节|下一个视频|下一讲|下一课|下一单元|'
                     r'Next\s+(?:lesson|chapter|unit|activity|video))\s*[›»→]?\s*$', re.I)
BLOCK_RE = re.compile(r'验证码|人机验证|安全验证|请先登录|登录已过期|重新登录|'
                      r'身份验证|人脸识别|签到|章节测验|提交答案|继续答题')
AUTH_RE = re.compile(r'验证码|人机验证|安全验证|请先登录|登录已过期|重新登录|'
                     r'身份验证|人脸识别|签到')


def _navigation_errors(action):
    """切节时的页面异常不能只显示“Error”，要给出可执行的下一步。"""
    def wrap(function):
        @wraps(function)
        def run(self, *args, **kwargs):
            try:
                return function(self, *args, **kwargs)
            except ManualRequired:
                raise
            except Exception as exc:
                if is_loading_error(exc):
                    raise ManualRequired('课程页面仍在加载或切换，请稍后选择继续。') from None
                raise ManualRequired(
                    f'{action}时页面操作失败（{type(exc).__name__}）；'
                    '请手动点右侧目录的下一项后选择继续，或用“定位目录”重新指定。') from None
        return run
    return wrap


class Adapter:
    def __init__(self, page, platform, next_selector=None, video_selector='video', control=None,
                 playback_rate=None):
        from core import validate_playback_rate
        self.playback_rate = None if playback_rate is None else validate_playback_rate(playback_rate)
        self.rate_reports = {}
        self.page = page
        self.platform = detect_platform(page.url) if platform == 'generic' else platform
        self.course_host = urlsplit(page.url).hostname
        self.navigator = Navigator(self)
        self.next_selector = next_selector
        self.video_selector = video_selector
        self.control = control
        self.dialog_pending = False
        self.quiz_dialog_handler = None
        self.advance_attempts = set()
        self.start_attempts = set()
        self.start_times = {}
        self.start_retries = {}
        self.pending_start_slots = set()
        self.activity_identity = None
        self.course_handled = None
        self.activity_completed = None
        page.set_default_timeout(1500)
        page.on('dialog', self._on_dialog)

    def _on_dialog(self, dialog):
        # 只把当前已发起的交卷确认交给小测处理器；其他弹窗仍留给用户。
        if self.quiz_dialog_handler is not None:
            try:
                if self.quiz_dialog_handler(dialog):
                    self.dialog_pending = False
                    return
            except Exception:
                pass
        self.dialog_pending = True

    def visible_frames(self):
        result = []
        for frame in self.page.frames:
            current = frame
            visible = True
            while current.parent_frame is not None:
                if not current.frame_element().is_visible():
                    visible = False
                    break
                current = current.parent_frame
            if visible:
                result.append(frame)
        return result

    @retry_dom_read
    def videos(self):
        result = []
        identity = self.content_identity()
        for frame in self.visible_frames():
            for index, locator in enumerate(frame.locator(self.video_selector).all()):
                if not locator.is_visible():
                    continue
                data = locator.evaluate(VIDEO_JS)
                # DOM 中的媒体标识仅哈希使用，不输出下载地址。
                material = '|'.join((identity, page_identity(frame.url),
                                     page_identity(data['source']), data['id'], str(index)))
                key = hashlib.sha256(material.encode()).hexdigest()[:32]
                # 懒加载会把空媒体地址变成实际地址；这仍是同一次启动。
                slot_material = '|'.join((identity, page_identity(frame.url),
                                          data['id'], str(index)))
                slot = hashlib.sha256(slot_material.encode()).hexdigest()[:32]
                if slot in self.pending_start_slots:
                    self.start_attempts.add(key)
                    # 懒加载后媒体地址变化会产生新 key，这里同步记录尝试时间，
                    # 否则新 key 会被误当成“从未尝试过”。
                    self.start_times.setdefault(key, time.monotonic())
                    if data['ready'] >= 1:
                        self.pending_start_slots.discard(slot)
                result.append(Video(locator, frame, VideoState(
                    key=key, current=data['current'], duration=data['duration'],
                    paused=data['paused'], ended=data['ended'],
                    ready=data['ready'], error=data['error'],
                    playback_rate=data.get('playback_rate', 1.0)), slot))
        return result

    def apply_playback_rate(self, video):
        """只修改选中媒体的标准倍速属性；拒绝或重置后不循环强制覆盖。"""
        if self.playback_rate is None or video.state.ready < 1 or video.state.ended:
            return video
        if self.control is not None:
            self.control.check()
        from core import validate_playback_rate
        from dataclasses import replace
        rate = validate_playback_rate(self.playback_rate)
        result = video.locator.evaluate(PLAYBACK_RATE_JS, {'key': video.state.key, 'rate': rate})
        actual = result['actual']
        video.state = replace(video.state, playback_rate=actual)
        previous = self.rate_reports.get(video.state.key)
        if result['attempted'] or previous != actual:
            if result['rejected'] or actual != rate:
                logging.warning('播放器未保持所选 %g 倍速，实际为 %g 倍；按实际速度继续，不重复强制设置。',
                                rate, actual)
            else:
                logging.info('视频播放速度：%g 倍。', actual)
            self.rate_reports[video.state.key] = actual
        return video

    def content_identity(self):
        return page_identity(self.page.url) + '|' + self.navigator.identity()

    def check_navigation_url(self, url):
        if not url:
            return
        url = urljoin(self.page.url, url)
        try:
            validate_url(url, self.platform)
            if self.platform == 'generic' and urlsplit(url).hostname != self.course_host:
                raise ValueError('different host')
        except ValueError:
            raise ManualRequired('下一项地址离开了当前课程站点，请在浏览器中确认后重新添加课程。') from None

    @retry_dom_read
    def guard(self, include_course=True):
        if self.dialog_pending:
            return '浏览器出现弹窗，请手动处理后继续。'
        try:
            validate_url(self.page.url, self.platform)
        except ValueError:
            return '页面已离开所选课程平台，请确认登录和当前地址。'
        if self.platform == 'generic' and urlsplit(self.page.url).hostname != self.course_host:
            return '页面已离开当前课程站点，请确认地址后重新添加课程。'
        if self.platform == 'uai' and urlsplit(self.page.url).hostname == 'sso.unipus.cn':
            return '当前仍在 U校园 AI 登录页，请完成登录并打开具体课程章节。'
        if include_course and self.course_handled is not None and self.course_handled():
            include_course = False
        for frame in self.visible_frames():
            # 只在可见交互控件中检测，不匹配目录里的课程标题。
            if frame.locator('input[type=password]:visible').count():
                return '页面有登录表单，请手动处理。'
            if include_course and frame.locator(
                    'input[type=radio]:visible, input[type=checkbox]:visible, '
                    '[role=radio]:visible, [role=checkbox]:visible').count():
                return '页面有需要处理的选项，请检查小测是否已识别。'
            controls = frame.locator(
                '[role=dialog]:visible, dialog[open]:visible, '
                'button:visible, [role=button]:visible, '
                'input[placeholder*="验证码"]:visible, iframe[title*="验证"]:visible'
            )
            for control in controls.all():
                try:
                    tag = control.evaluate('e => e.tagName')
                    text = control.inner_text() if tag != 'INPUT' else ''
                    label = control.get_attribute('aria-label') or ''
                    hint = (control.get_attribute('placeholder')
                            or control.get_attribute('title') or '')
                except Exception:
                    # 页面正在跳转时元素会失效；跳过这一个，不当作致命错误。
                    continue
                if (BLOCK_RE if include_course else AUTH_RE).search(text + label + hint):
                    return '检测到登录、验证或课程交互，请手动处理。'
        return None

    def task_pending(self):
        if self.platform != 'chaoxing':
            return False
        return any(frame.locator('[aria-label="任务点未完成"]:visible').count()
                   for frame in self.visible_frames())

    def activity_key(self):
        """活动身份；页面切换中读不到时返回空串，交给调用方等待。"""
        try:
            return self.activity_identity()
        except Exception as exc:
            if is_loading_error(exc):
                return ''
            raise

    def _task_pending_settled(self, seconds=10):
        """提交后平台常延迟更新任务点；仍显示未完成再暂停。"""
        if not self.task_pending():
            return False
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.control is not None:
                self.control.check()
                self.control.sleep(1)
            else:
                time.sleep(1)
            if not self.task_pending():
                return False
        return True

    def foreground_state(self):
        """读取浏览器报告的焦点；活跃模拟下不代表操作系统的前台窗口。"""
        from playwright.sync_api import TimeoutError as BrowserTimeout
        predicates = (
            (True, "() => document.visibilityState === 'visible' && document.hasFocus()"),
            (False, "() => document.visibilityState !== 'visible' || !document.hasFocus()"))
        # evaluate 不受页面默认超时约束，原生弹窗可使其一直等待。
        # 分别确认获焦/失焦，两个谓词均超时代表未知，不能推断为失焦。
        for focused, predicate in predicates:
            if self.dialog_pending:
                raise ManualRequired('浏览器出现弹窗，请手动处理后继续。')
            confirmed = False
            try:
                handle = self.page.wait_for_function(predicate, timeout=750)
                confirmed = True
                handle.dispose()
            except BrowserTimeout:
                pass
            except Exception:
                if self.dialog_pending:
                    raise ManualRequired('浏览器出现弹窗，请手动处理后继续。') from None
                return None
            if self.dialog_pending:
                raise ManualRequired('浏览器出现弹窗，请手动处理后继续。')
            if confirmed:
                return focused
        return None

    def recover_foreground_pause(self, video_key, poll, control=None):
        if self.foreground_state() is not False:
            return False
        message = ('视频已暂停，等待切回课程窗口。请点击课程页面并保持它在前台；'
                   '返回后尝试恢复一次。要求焦点的课程请轮流播放。')
        logging.info('%s', message)
        if control is not None:
            control.emit('status', message=message)
        wait = control.sleep if control is not None else time.sleep
        while True:
            if control is not None:
                control.check()
            if self.page.is_closed():
                raise KeyboardInterrupt
            wait(poll)
            focused = self.foreground_state()
            if focused is None:
                raise ManualRequired('暂时无法读取课程窗口焦点，请检查页面后继续。')
            if focused:
                break
        reason = self.guard()
        if reason:
            raise ManualRequired(reason)
        video = only_one(self.videos(), '可见视频')
        sampled_at = time.monotonic()
        if video is None or video.state.key != video_key or video.state.ended:
            raise ManualRequired('等待焦点期间视频已切换或结束，请确认页面与进度后继续。')
        if video.state.error:
            raise ManualRequired('播放器报告媒体错误，请检查页面。')
        if video.state.paused:
            self.play(video, resume_from_focus=True)
        logging.info('课程窗口已获得焦点，继续观察正常播放进度。')
        return video.state, sampled_at

    def rate_rejected(self, key):
        """播放器是否已把所选倍速重置（不接受倍速）。"""
        if self.playback_rate is None:
            return False
        actual = self.rate_reports.get(key)
        return actual is not None and actual != self.playback_rate

    def recover_rate_rejection(self, video_key, poll, control=None):
        """播放器拒绝所选倍速（重置为实际值）并暂停：自动恢复按实际速度播放。"""
        reason = self.guard()
        if reason:
            raise ManualRequired(reason)
        video = only_one(self.videos(), '可见视频')
        sampled_at = time.monotonic()
        if video is None or video.state.key != video_key or video.state.ended:
            raise ManualRequired('视频状态已变化，请确认页面后继续。')
        if video.state.error:
            raise ManualRequired('播放器报告媒体错误，请检查页面。')
        if video.state.paused:
            self.play(video, resume_from_focus=True)
        logging.info('播放器未保持所选倍速，已按实际速度恢复播放。')
        return video.state, sampled_at

    def _request_load(self, video):
        try:
            video.locator.evaluate('''v => {
              try { v.preload = 'auto'; } catch (_) {}
              try { if (v.readyState === 0) v.load(); } catch (_) {}
            }''')
        except Exception:
            pass

    def play(self, video, *, resume_from_focus=False):
        reason = self.guard()
        if reason:
            raise ManualRequired(reason)
        # 先把加载请求发给播放器：preload=none 的播放器收到 load() 才会取元数据，
        # 这是幂等操作，不会像重复点击那样把视频暂停。
        self._request_load(video)
        now = time.monotonic()
        key = video.state.key
        last = self.start_times.get(key, 0.0)
        retries = self.start_retries.get(key, 0)
        already = key in self.start_attempts
        if already and not resume_from_focus:
            # 媒体地址改变会重建观察状态；加载中的启动请求仍然有效。
            if video.state.ready < 3:
                return
            # 播放器已加载但仍暂停：间隔 3 秒后允许再试，最多三次。
            # 视频正在正常播放时不再重复点击，避免把它点成暂停。
            if not (video.state.paused and retries < 3 and now - last >= 3):
                raise ManualRequired('已尝试启动本视频，请亲自点击播放后选择继续。')
        # 优先使用页面原有播放按钮，让播放器照常处理用户操作。
        controls = [item for item in video.frame.get_by_role(
            'button', name=re.compile(r'^\s*(播放|播放视频|Play)\s*$', re.I)).all()
                    if item.is_visible() and item.is_enabled()]
        button = only_one(controls, '播放按钮')
        targets = [button] if button is not None else []
        if button is None:
            # 播放器常用覆盖层元素做播放按钮；一次查询避免同一元素被多个选择器重复命中。
            selector = ', '.join(PLAY_BUTTON_SELECTORS)
            overlays = [item for item in video.frame.locator(selector).all()
                        if item.is_visible() and item.is_enabled()]
            button = only_one(overlays, '播放器播放控件')
            targets = overlays if button is None else [button]
        # 点击可能已经送达页面，但响应丢失；登记后不再自动重发。
        self.start_attempts.add(key)
        self.start_times[key] = now
        if already:
            self.start_retries[key] = retries + 1
        if video.slot and video.state.ready < 1:
            self.pending_start_slots.add(video.slot)
        if len(targets) > 1:
            # 多个候选时逐个尝试并确认是否真的开始播放，避免点到装饰元素后就此放弃。
            for index, target in enumerate(targets):
                try:
                    target.click(timeout=3000)
                except Exception:
                    if index == 0:
                        raise
                    continue
                if self._playing_within(video, 1.5):
                    return
            raise ManualRequired('已点击播放控件，但播放器仍未开始；请亲自点击播放后选择继续。')
        if targets:
            targets[0].click(timeout=3000)
            # 点完确认一下是否真的开始播放；没播再换别的办法（点错元素时很常见）。
            if self._playing_within(video, 1.5):
                return
            logging.info('播放控件未生效，改试点击画面与媒体接口（候选 %s 个）', len(targets))
        # 平台播放器常把播放绑定在画面点击上，且只认真实手势；
        # Playwright 的 click 走 CDP 输入，事件是 trusted，等同于用户点击。
        if self._click_video_area(video):
            return
        if self._fallback_play(video):
            return
        if targets:
            # 点击已经送达页面，交给上层重试；不在这里判死刑。
            return
        raise ManualRequired('播放器拒绝自动播放，请亲自点击播放后选择继续。')

    def _click_video_area(self, video):
        """点击视频画面/控制栏播放键：真实输入事件，播放器会当作用户操作。"""
        try:
            box = video.locator.bounding_box()
        except Exception:
            box = None
        points = []
        if box and box.get('width', 0) > 8 and box.get('height', 0) > 8:
            points.append({'x': box['width'] / 2, 'y': box['height'] / 2})
            points.append({'x': 24, 'y': max(8, box['height'] - 24)})
        for point in points:
            try:
                video.locator.click(position=point, timeout=3000)
            except Exception:
                continue
            if self._playing_within(video, 1.2):
                logging.info('点击视频画面后开始播放')
                return True
        return False

    def _fallback_play(self, video):
        """媒体接口启动：先普通 play()，再静音 play()（浏览器对静音更宽松）。"""
        outcome = self._evaluate_play(video)
        if outcome == 'ok' and self._playing_within(video, 1.5):
            return True
        if outcome == 'error':
            return False
        muted = self._play_muted(video)
        if muted == 'ok' and self._playing_within(video, 1.5):
            logging.info('播放器只允许静音自动播放，已保持静音继续')
            return True
        return False

    def _evaluate_play(self, video):
        try:
            return video.locator.evaluate("""v => Promise.race([
                v.play().then(() => 'ok').catch(() => 'blocked'),
                new Promise(resolve => setTimeout(() => resolve('timeout'), 3000))
            ])""")
        except Exception:
            return 'error'

    def _play_muted(self, video):
        try:
            result = video.locator.evaluate("""v => (async () => {
              try { v.muted = true; } catch (_) {}
              try {
                return await Promise.race([
                  v.play().then(() => 'ok').catch(() => 'blocked'),
                  new Promise(resolve => setTimeout(() => resolve('timeout'), 3000)),
                ]);
              } catch (_) { return 'error'; }
            })()""")
        except Exception:
            return 'error'
        return result if isinstance(result, str) else 'error'

    def _playing_within(self, video, seconds):
        """点击后短时间确认是否真的开始播放。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                state = video.locator.evaluate(VIDEO_JS)
            except Exception:
                return False
            if not state.get('paused', True) or state.get('current', 0) > 0:
                return True
            if self.control is not None:
                self.control.check()
            time.sleep(0.25)
        return False

    def pause(self):
        if self.dialog_pending:
            # 原生弹窗会阻塞 DOM 读取；保留弹窗供处理，同时允许停止任务。
            return
        try:
            for video in self.videos():
                video.locator.evaluate('v => v.pause()')
        except Exception:
            # 已关闭页面/弹窗阻塞时可能无法暂停；不妨碍保存和退出。
            pass


    def next_control(self):
        candidates = []
        def usable(items):
            found = []
            for item in items:
                if item.is_visible() and item.is_enabled():
                    disabled = item.get_attribute('aria-disabled') == 'true'
                    classes = (item.get_attribute('class') or '').lower()
                    if not disabled and 'disabled' not in classes:
                        found.append(item)
            return found

        for frame in self.visible_frames():
            if self.next_selector:
                candidates += usable(frame.locator(self.next_selector).all())
            elif self.platform == 'chaoxing':
                # 已在用户打开的超星页面只读核实。
                candidates += usable(frame.locator('#prevNextFocusNext').all())
            else:
                items = frame.get_by_role('button', name=NEXT_RE).all()
                items += frame.get_by_role('link', name=NEXT_RE).all()
                candidates += usable(items)
        if not candidates and self.next_selector is None:
            # 固定 id 未命中时再按文字查找；只在唯一可用时使用。
            for frame in self.visible_frames():
                items = frame.get_by_role('link', name=NEXT_RE).all()
                items += frame.get_by_role('button', name=NEXT_RE).all()
                candidates += usable(items)
        button = only_one(candidates, '下一节按钮')
        if button is not None:
            href = button.get_attribute('href') or ''
            if href and callable(getattr(self.page, 'evaluate', None)):
                href = button.evaluate('e => e.href || e.getAttribute("href")')
            self.check_navigation_url(href)
            if callable(getattr(self.page, 'evaluate', None)):
                return NavigationTarget(self.navigator, button)
            return button
        return self.navigator.next_target()

    def has_activity_tab(self):
        """本节是否还有待进入的活动选项卡；只读探测，不点击。"""
        if self.activity_identity is None:
            return False
        for frame in self.visible_frames():
            try:
                result = frame.evaluate(ACTIVITY_TABS_JS)
            except Exception:
                continue
            if isinstance(result, dict) and result.get('count') == 1:
                try:
                    frame.locator('[data-ch-tab="1"]').evaluate(
                        '(e) => e.removeAttribute("data-ch-tab")')
                except Exception:
                    pass
                return True
        return False

    def switch_activity_tab(self, old_key, timeout=30):
        """同一节内的“视频 / 章节测验”选项卡。

        返回 None 表示本节没有可切换的活动选项卡；True 已切换；False 点了但页面没反应。
        """
        if self.activity_identity is None:
            return None
        for frame in self.visible_frames():
            try:
                result = frame.evaluate(ACTIVITY_TABS_JS)
            except Exception:
                continue
            if not isinstance(result, dict):
                continue
            if result.get('ambiguous'):
                raise ManualRequired('本节有多组选项卡，无法确定下一个活动。请点开本节待完成的活动后继续。')
            if result.get('count') != 1:
                continue
            text = str(result.get('text') or '')
            mark = '[data-ch-tab="1"]'
            marker = frame.locator(mark)
            if marker.count() != 1:
                continue
            snapshot = self._tab_snapshot()
            try:
                self.advance_attempts.add('tab:' + old_key)
                if not self._click_tab(marker, 'self'):
                    return False
                if self.control is not None:
                    self.control.emit('status', message=f'正在切换到本节的活动：{text or "下一项"}')
                logging.info('本节还有活动选项卡，已点击：%s', text)
                if self._tab_switched(frame, text, old_key, snapshot, timeout):
                    return True
                # 有的平台把点击事件绑在内层元素或外层容器上，换一个目标再试一次。
                for mode in ('js', 'child', 'parent'):
                    if marker.count() != 1:
                        break
                    if not self._click_tab(marker, mode):
                        continue
                    logging.info('本节活动选项卡：改用%s方式重新点击',
                                 {'js': '脚本', 'child': '内层', 'parent': '外层'}[mode])
                    if self._tab_switched(frame, text, old_key, snapshot, max(5, timeout // 2)):
                        return True
                return False
            finally:
                self._clear_tab_mark(marker)
        return None

    def _click_tab(self, marker, mode='self'):
        try:
            if self.control is not None:
                self.control.check()
        except Exception:
            return False
        try:
            if mode == 'js':
                # 元素被遮挡或可点击性检查不通过时，直接触发原生 click()。
                marker.evaluate('(e) => e.click()')
                return True
            if mode == 'child':
                inner = marker.locator('*').first
                if inner.count() == 1:
                    inner.click(timeout=3000)
                    return True
                return False
            if mode == 'parent':
                parent = marker.locator('xpath=..')
                if parent.count() == 1:
                    parent.click(timeout=3000)
                    return True
                return False
            marker.click(timeout=5000)
            return True
        except Exception:
            return False

    def _tab_snapshot(self):
        try:
            frames = sorted(page.url for page in self.page.frames)
        except Exception:
            frames = []
        try:
            pages = len(self.page.context.pages)
        except Exception:
            pages = 0
        return {'url': self.page.url, 'frames': frames, 'pages': pages,
                'prompt': self._quiz_prompt_visible(self.page.main_frame),
                'length': self._visible_text_length()}

    def _quiz_prompt_visible(self, frame):
        try:
            return bool(frame.evaluate(TAB_PROMPT_JS))
        except Exception:
            return False

    def _visible_text_length(self):
        try:
            return int(self.page.main_frame.evaluate(
                '() => document.body ? document.body.innerText.length : 0'))
        except Exception:
            return 0

    def _tab_switched(self, frame, text, old_key, before, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.control is not None:
                self.control.check()
            if self._tab_signal(frame, text, old_key, before):
                return True
            if self.control is not None:
                self.control.sleep(1)
            else:
                time.sleep(1)
        return False

    def _tab_signal(self, frame, text, old_key, before):
        """任何一个可靠信号成立都算已切换，避免因答题区加载慢而判定失败。"""
        if text:
            try:
                if frame.evaluate(TAB_ACTIVE_JS, text):
                    return True
            except Exception:
                pass
        if self.page.url != before['url']:
            return True
        try:
            frames = sorted(page.url for page in self.page.frames)
        except Exception:
            frames = before['frames']
        if frames != before['frames']:
            return True
        try:
            pages = len(self.page.context.pages)
        except Exception:
            pages = before.get('pages', 0)
        if pages > before.get('pages', pages):
            # 新标签页打开时原页面不会变化，再点也不会有效果。
            raise ManualRequired(
                '本节的下一个活动在新标签页中打开；请到新标签页作答，处理后再选择继续。')
        # 点开后出现“开始答题/进入测验”的入口，说明已经进到测验页（可能还在加载题目）。
        if self._quiz_prompt_visible(frame) and not before.get('prompt'):
            return True
        if abs(self._visible_text_length() - before['length']) >= 200:
            return True
        current = self.activity_key()
        return bool(current and current != old_key)

    def _clear_tab_mark(self, target):
        try:
            target.evaluate('(e) => e.removeAttribute("data-ch-tab")')
        except Exception:
            pass

    def _advance_within_section(self, old_key, timeout, still_on_source):
        # 标签高亮可以先于课件更新。只要原课件还在，重试也不能落到“下一节”。
        token = 'tab:' + old_key
        if token not in self.advance_attempts:
            switched = self.switch_activity_tab(old_key, min(timeout, 30))
            if switched is None:
                return False
            self.advance_attempts.add(token)
            if not switched:
                raise ManualRequired('已尝试切换本节活动，但内容未更新。请点开本节待完成的活动后继续。')
        deadline = time.monotonic() + timeout
        while still_on_source():
            if time.monotonic() >= deadline:
                raise ManualRequired('本节下一个活动尚未加载，仍显示原课件。请点开本节待完成的活动后继续。')
            if self.control is not None:
                self.control.sleep(0.3)
            else:
                time.sleep(0.3)
        return True

    @_navigation_errors('切换到下一节')
    def advance(self, old_key, timeout=30):
        """点击一次后等待章节变化；失败就停，不重试点击而跳过章节。"""
        self.navigator.ensure_ready()
        reason = self.guard() if self.activity_identity is None else self.guard(include_course=False)
        if reason:
            raise ManualRequired(reason)
        if self.activity_identity is not None and self.activity_key():
            return
        current = only_one(self.videos(), '可见视频')
        if current and current.state.key != old_key:
            return
        if current is None or not current.state.ended:
            raise ManualRequired('当前视频状态已变化，请确认页面后继续。')
        if old_key in self.advance_attempts:
            raise ManualRequired('本节已尝试点击下一节，请在页面中手动切到新章节后选择继续。')
        # 同一节可能还有“章节测验”等其他活动；先切过去，再考虑下一节。
        def same_video():
            return not self.activity_key() and any(v.state.key == old_key for v in self.videos())
        if self.activity_identity is not None and self._advance_within_section(old_key, timeout, same_video):
            return
        if self._task_pending_settled():
            raise ManualRequired('视频已结束，但平台仍显示任务点未完成。')
        button = self.next_control()
        if button is None:
            raise ManualRequired('未找到下一节按钮或可识别的目录。可使用“定位目录”；课程已结束时选择结束本课程。')
        self.advance_attempts.add(old_key)
        before_url = self.page.url
        button.click(timeout=5000)
        self.navigator.wait_ready(timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.control is not None:
                self.control.check()
            reason = self.guard() if self.activity_identity is None else self.guard(include_course=False)
            if reason:
                raise ManualRequired(reason)
            if self.page.url != before_url:
                return
            if self.activity_identity is not None and self.activity_key():
                return
            video = only_one(self.videos(), '可见视频')
            if video and video.state.key != old_key:
                return
            if self.control is not None:
                self.control.sleep(1)
            else:
                time.sleep(1)
        raise ManualRequired('下一页没有出现新的可识别视频，可能是文字或测验，请手动处理。')

    @_navigation_errors('切换到下一节')
    def advance_activity(self, old_key, timeout=30):
        """非视频活动完成后只点一次下一节，确认新活动或视频出现。"""
        self.navigator.ensure_ready()
        reason = self.guard(include_course=False)
        if reason:
            raise ManualRequired(reason)
        if not old_key or self.activity_identity is None:
            raise ManualRequired('当前活动已变化，请确认页面后继续。')
        current_key = self.activity_key()
        if current_key and current_key != old_key:
            # 交卷后题目指纹会变化，或结果页被识别成文档，得到新的 key；
            # 只要这个新 key 仍是已提交/已完成的活动，就继续切下一节，不当作已切换。
            completed = self.activity_completed
            if not (callable(completed) and completed(current_key)):
                return
        videos = self.videos()
        if not current_key and any(not video.state.ended for video in videos):
            return
        token = 'activity:' + old_key
        if token in self.advance_attempts:
            raise ManualRequired('本页已尝试切换，请检查页面或手动进入下一节，不会重复点击。')
        # 小测完成后本节可能还有后续活动选项卡；有则先切，没有才切目录下一节。
        def same_activity():
            return self.activity_key() == current_key and not any(not v.state.ended for v in self.videos())
        if self._advance_within_section(old_key, timeout, same_activity):
            return
        if self._task_pending_settled():
            raise ManualRequired('平台仍显示任务点未完成，请检查当前文档或小测的完成要求。')
        button = self.next_control()
        if button is None:
            raise ManualRequired('未找到下一节按钮或可识别的目录。可使用“定位目录”；课程已结束时选择结束本课程。')
        previous_videos = {video.state.key for video in videos}
        before_url = self.page.url
        self.advance_attempts.add(token)
        button.click(timeout=5000)
        self.navigator.wait_ready(timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.control is not None:
                self.control.check()
            reason = self.guard(include_course=False)
            if reason:
                raise ManualRequired(reason)
            if self.page.url != before_url:
                return
            current_key = self.activity_key()
            if current_key and current_key != old_key:
                return
            if not current_key and any(video.state.key not in previous_videos
                                       for video in self.videos()):
                return
            if self.control is not None:
                self.control.sleep(1)
            else:
                time.sleep(1)
        raise ManualRequired('切换后未识别到新的内容，请检查课程页面。')

    def inspect(self):
        """报告只含页面结构和播放状态，不导出 cookie、签名或媒体地址。"""
        from quiz_screenshots import structure_report as quiz_report
        report = {'platform': self.platform, 'page': self.page.url.split('?')[0].split('#')[0],
                  'frames': [], 'guard': self.guard()}
        for frame in self.visible_frames():
            videos = frame.locator('video').all()
            tabs = {}
            try:
                tabs = frame.evaluate(ACTIVITY_TABS_JS) or {}
            except Exception:
                tabs = {}
            if isinstance(tabs, dict) and tabs.get('count') == 1:
                try:
                    frame.locator('[data-ch-tab="1"]').evaluate(
                        '(e) => e.removeAttribute("data-ch-tab")')
                except Exception:
                    pass
            report['frames'].append({
                'origin_path': frame.url.split('?')[0].split('#')[0],
                'videos': [v.evaluate(VIDEO_JS) for v in videos if v.is_visible()],
                'next_labels': [v.inner_text()[:60] for v in frame.get_by_role('button', name=NEXT_RE).all()
                                if v.is_visible()],
                'pending_tasks': frame.locator('[aria-label="任务点未完成"]:visible').count(),
                'quiz': quiz_report(frame),
                'activity_tabs': tabs,
            })
        for frame in report['frames']:
            for video in frame['videos']:
                video.pop('source', None)
        try:
            report['has_next'] = self.next_control() is not None
            directory = self.navigator.state()
            if directory:
                index = directory['current']
                report['directory'] = {'current': index + 1, 'total': len(directory['items']),
                    'title': directory['items'][index]['text'],
                    'next_title': (directory['items'][index + 1]['text']
                                   if index + 1 < len(directory['items']) else ''),
                    'located_by_user': bool(self.navigator.rule)}
            else:
                report['directory'] = None
        except ManualRequired as exc:
            report['has_next'] = False
            report['next_warning'] = str(exc)
        return report
