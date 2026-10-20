"""页面适配：只操作可见播放器和明确的下一节控件。"""
from dataclasses import dataclass
import hashlib
import logging
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from core import VideoState, validate_url, detect_platform
from navigation import Navigator, NavigationTarget


class ManualRequired(RuntimeError):
    pass


BACKGROUND_JS = r"""(() => {
  const installed = Symbol.for('courseHelper.backgroundPlayback');
  if (window[installed]) return true;
  Object.defineProperty(window, installed, {value: true});
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
  error: v.error ? v.error.code : 0
})"""

NEXT_RE = re.compile(r'^\s*(下一节|下一章节|下一个视频|下一讲|下一课|下一单元|'
                     r'Next\s+(?:lesson|chapter|unit|activity|video))\s*[›»→]?\s*$', re.I)
BLOCK_RE = re.compile(r'验证码|人机验证|安全验证|请先登录|登录已过期|重新登录|'
                      r'身份验证|人脸识别|签到|章节测验|提交答案|继续答题')
AUTH_RE = re.compile(r'验证码|人机验证|安全验证|请先登录|登录已过期|重新登录|'
                     r'身份验证|人脸识别|签到')


class Adapter:
    def __init__(self, page, platform, next_selector=None, video_selector='video', control=None):
        self.page = page
        self.platform = detect_platform(page.url) if platform == 'generic' else platform
        self.course_host = urlsplit(page.url).hostname
        self.navigator = Navigator(self)
        self.next_selector = next_selector
        self.video_selector = video_selector
        self.control = control
        self.dialog_pending = False
        self.advance_attempts = set()
        self.start_attempts = set()
        self.pending_start_slots = set()
        self.activity_identity = None
        self.course_handled = None
        page.set_default_timeout(1500)
        page.on('dialog', self._on_dialog)

    def _on_dialog(self, dialog):
        # 保持弹窗打开，让用户直接处理，不自动点击确认。
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
                    if data['ready'] >= 1:
                        self.pending_start_slots.discard(slot)
                result.append(Video(locator, frame, VideoState(
                    key=key, current=data['current'], duration=data['duration'],
                    paused=data['paused'], ended=data['ended'],
                    ready=data['ready'], error=data['error']), slot))
        return result

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
                text = control.inner_text() if control.evaluate('e => e.tagName') != 'INPUT' else ''
                label = control.get_attribute('aria-label') or ''
                hint = control.get_attribute('placeholder') or control.get_attribute('title') or ''
                if (BLOCK_RE if include_course else AUTH_RE).search(text + label + hint):
                    return '检测到登录、验证或课程交互，请手动处理。'
        return None

    def task_pending(self):
        if self.platform != 'chaoxing':
            return False
        return any(frame.locator('[aria-label="任务点未完成"]:visible').count()
                   for frame in self.visible_frames())

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

    def play(self, video, *, resume_from_focus=False):
        reason = self.guard()
        if reason:
            raise ManualRequired(reason)
        if video.state.key in self.start_attempts and not resume_from_focus:
            # 媒体地址改变会重建观察状态；加载中的启动请求仍然有效。
            # 不再次点击，也不提前打断正在异步加载的播放器。
            if video.state.ready < 3:
                return
            raise ManualRequired('已尝试启动本视频，请亲自点击播放后选择继续。')
        # 优先使用页面原有播放按钮，让播放器照常处理用户操作。
        controls = [item for item in video.frame.get_by_role(
            'button', name=re.compile(r'^\s*(播放|播放视频|Play)\s*$', re.I)).all()
                    if item.is_visible() and item.is_enabled()]
        button = only_one(controls, '播放按钮')
        # 点击可能已经送达页面，但响应丢失；登记后不再自动重发。
        self.start_attempts.add(video.state.key)
        if video.slot and video.state.ready < 1:
            self.pending_start_slots.add(video.slot)
        if button:
            button.click(timeout=3000)
        else:
            # 原生 play 仍受浏览器的自动播放策略限制，不修改策略或倍速。
            result = video.locator.evaluate("""v => Promise.race([
                v.play().then(() => 'ok').catch(() => 'blocked'),
                new Promise(resolve => setTimeout(() => resolve('timeout'), 3000))
            ])""")
            if result != 'ok':
                raise ManualRequired('浏览器未允许播放，请亲自点击播放后选择继续。')

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
        for frame in self.visible_frames():
            if self.next_selector:
                items = frame.locator(self.next_selector).all()
            elif self.platform == 'chaoxing':
                # 已在用户打开的超星页面只读核实。
                items = frame.locator('#prevNextFocusNext').all()
            else:
                items = frame.get_by_role('button', name=NEXT_RE).all()
                items += frame.get_by_role('link', name=NEXT_RE).all()
            for item in items:
                if item.is_visible() and item.is_enabled():
                    disabled = item.get_attribute('aria-disabled') == 'true'
                    classes = (item.get_attribute('class') or '').lower()
                    if not disabled and 'disabled' not in classes:
                        candidates.append(item)
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

    def advance(self, old_key, timeout=30):
        """点击一次后等待章节变化；失败就停，不重试点击而跳过章节。"""
        self.navigator.ensure_ready()
        reason = self.guard() if self.activity_identity is None else self.guard(include_course=False)
        if reason:
            raise ManualRequired(reason)
        if self.activity_identity is not None and self.activity_identity():
            return
        current = only_one(self.videos(), '可见视频')
        if current and current.state.key != old_key:
            return
        if current is None or not current.state.ended:
            raise ManualRequired('当前视频状态已变化，请确认页面后继续。')
        if old_key in self.advance_attempts:
            raise ManualRequired('本节已尝试点击下一节，请在页面中手动切到新章节后选择继续。')
        if self.task_pending():
            raise ManualRequired('视频已结束，但平台仍显示任务点未完成。')
        button = self.next_control()
        if button is None:
            raise ManualRequired('未找到下一节按钮或可识别的目录。可使用“定位目录”；课程已结束时选择结束本课程。')
        self.advance_attempts.add(old_key)
        button.click(timeout=5000)
        self.navigator.wait_ready(timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.control is not None:
                self.control.check()
            reason = self.guard() if self.activity_identity is None else self.guard(include_course=False)
            if reason:
                raise ManualRequired(reason)
            if self.activity_identity is not None and self.activity_identity():
                return
            video = only_one(self.videos(), '可见视频')
            if video and video.state.key != old_key:
                return
            if self.control is not None:
                self.control.sleep(1)
            else:
                time.sleep(1)
        raise ManualRequired('下一页没有出现新的可识别视频，可能是文字或测验，请手动处理。')

    def advance_activity(self, old_key, timeout=30):
        """非视频活动完成后只点一次下一节，确认新活动或视频出现。"""
        self.navigator.ensure_ready()
        reason = self.guard(include_course=False)
        if reason:
            raise ManualRequired(reason)
        if not old_key or self.activity_identity is None:
            raise ManualRequired('当前活动已变化，请确认页面后继续。')
        current_key = self.activity_identity()
        if current_key and current_key != old_key:
            return
        videos = self.videos()
        if not current_key and any(not video.state.ended for video in videos):
            return
        token = 'activity:' + old_key
        if token in self.advance_attempts:
            raise ManualRequired('本页已尝试切换，请检查页面或手动进入下一节，不会重复点击。')
        if self.task_pending():
            raise ManualRequired('平台仍显示任务点未完成，请检查当前文档或小测的完成要求。')
        button = self.next_control()
        if button is None:
            raise ManualRequired('未找到下一节按钮或可识别的目录。可使用“定位目录”；课程已结束时选择结束本课程。')
        previous_videos = {video.state.key for video in videos}
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
            current_key = self.activity_identity()
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
        from quiz_dom import quiz_report
        report = {'platform': self.platform, 'page': self.page.url.split('?')[0].split('#')[0],
                  'frames': [], 'guard': self.guard()}
        for frame in self.visible_frames():
            videos = frame.locator('video').all()
            report['frames'].append({
                'origin_path': frame.url.split('?')[0].split('#')[0],
                'videos': [v.evaluate(VIDEO_JS) for v in videos if v.is_visible()],
                'next_labels': [v.inner_text()[:60] for v in frame.get_by_role('button', name=NEXT_RE).all()
                                if v.is_visible()],
                'pending_tasks': frame.locator('[aria-label="任务点未完成"]:visible').count(),
                'quiz': quiz_report(frame),
            })
        for frame in report['frames']:
            for video in frame['videos']:
                video.pop('source', None)
        try:
            report['has_next'] = self.next_control() is not None
            directory = self.navigator.state()
            report['directory'] = ({'current': directory['current'] + 1,
                'total': len(directory['items']), 'title': directory['items'][directory['current']]['text'],
                'located_by_user': bool(self.navigator.rule)} if directory else None)
        except ManualRequired as exc:
            report['has_next'] = False
            report['next_warning'] = str(exc)
        return report
