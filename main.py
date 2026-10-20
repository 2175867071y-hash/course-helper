"""Windows 网课辅助脚本：python main.py --help。"""
import argparse
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import sys
import time

from browser import Adapter, ManualRequired, only_one, page_identity
from core import Monitor, Store, validate_url

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
START = {'chaoxing': 'https://i.chaoxing.com/', 'mooc': 'https://www.icourse163.org/',
         'uai': 'https://ucloud.unipus.cn/', 'generic': ''}


@dataclass
class Task:
    task_id: str
    platform: str
    url: str
    max_chapters: int = 0
    next_selector: str | None = None
    video_selector: str = 'video'


def load_tasks(path):
    content = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    entries = content.get('tasks') if isinstance(content, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError('任务文件必须包含非空 tasks 列表。')
    result, ids = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('每项任务必须是对象。')
        task_id = entry.get('id')
        if not isinstance(task_id, str) or not task_id.strip() or task_id in ids:
            raise ValueError('每个任务需要非空且不重复的 id。')
        platform = entry.get('platform')
        url = entry.get('url')
        if not isinstance(url, str):
            raise ValueError('任务缺少课程 url。')
        validate_url(url, platform)
        maximum = entry.get('max_chapters', 0)
        if type(maximum) is not int or not 0 <= maximum <= 1000:
            raise ValueError('max_chapters 必须是 0 到 1000 的整数，0 表示不限。')
        selector = entry.get('next_selector')
        video_selector = entry.get('video_selector', 'video')
        if selector is not None and (not isinstance(selector, str) or not selector.strip()):
            raise ValueError('next_selector 必须是非空 CSS 选择器。')
        if not isinstance(video_selector, str) or not video_selector.strip():
            raise ValueError('video_selector 必须是非空 CSS 选择器。')
        ids.add(task_id)
        result.append(Task(task_id, platform, url, maximum, selector, video_selector))
    return result


def ask_manual(adapter, reason, control=None):
    adapter.pause()
    logging.warning('%s', reason)
    if control is not None:
        choices = [('r', '已处理，继续'), ('s', '结束本课程'), ('q', '停止')]
        if isinstance(adapter, Adapter):
            choices.insert(1, ('locate', '定位目录'))
        choice = control.ask('需要你处理一下', reason, choices)
        if choice == 'locate':
            adapter.navigator.configure(control)
            choice = 'r'
        if choice == 'r' and isinstance(adapter, Adapter):
            adapter.navigator.acknowledge()
        adapter.dialog_pending = False
        return choice
    print('\a', end='', flush=True)
    while True:
        choice = input('处理页面后输入 r 继续，s 结束本任务，q 退出：').strip().lower()
        if choice in {'r', 's', 'q'}:
            if choice == 'r' and isinstance(adapter, Adapter):
                adapter.navigator.acknowledge()
            adapter.dialog_pending = False
            return choice


def run_task(adapter, task, store, poll, stall, control=None, *, activities=None):
    monitor = Monitor(stall_seconds=stall)
    missing_since = time.monotonic()
    printed_at = 0.0
    completed = 0
    previous = None
    previous_url = None
    pending_end = None
    wait = control.sleep if control is not None else time.sleep

    def manual(reason):
        if control is None:
            return ask_manual(adapter, reason)
        return ask_manual(adapter, reason, control)

    try:
        while True:
            try:
                if control is not None:
                    control.check()
                if activities is not None:
                    reason = adapter.guard(include_course=False)
                    if reason:
                        raise ManualRequired(reason)
                    activity_action = activities.process()
                    if activity_action is not None:
                        # 文档/小测不能继承上一视频的结束状态或观测记录。
                        pending_end = None
                        previous = previous_url = None
                        monitor = Monitor(stall_seconds=stall)
                        missing_since = time.monotonic()
                        if activity_action == 'done':
                            adapter.advance_activity(activities.identity())
                        elif activity_action != 'working':
                            raise ValueError('无法识别当前课程活动状态')
                        wait(poll)
                        continue
                reason = adapter.guard()
                if reason:
                    raise ManualRequired(reason)
                video = only_one(adapter.videos(), '可见视频')
                now = time.monotonic()
                if video is None:
                    if now - missing_since >= 30:
                        raise ManualRequired('30 秒内未找到可识别的视频、文档或题目。'
                                             '请打开具体内容；目录切换不正常时可使用“定位目录”。')
                    wait(poll)
                    continue
                missing_since = now
                state = video.state
                previous = state
                previous_url = adapter.page.url
                if pending_end and (state.key != pending_end or not state.ended):
                    pending_end = None
                    monitor = Monitor(stall_seconds=stall)
                action = 'finished' if pending_end else monitor.step(state, now)
                status = {'wait': 'observing', 'start': 'starting',
                          'manual': 'needs_user', 'finished': 'video_ended'}[action]
                store.save(task.task_id, previous_url, state, status)
                if control is not None:
                    control.emit('progress', current=state.current,
                                 duration=state.duration, completed=completed)
                    control.emit('status', message=monitor.reason)
                if now - printed_at >= 15:
                    logging.info('任务 %s | %.1f / %.1f 秒 | %s',
                                 task.task_id, state.current, state.duration, monitor.reason)
                    printed_at = now
                if action == 'manual':
                    if (monitor.reason == Monitor.PAUSED_REASON
                            and isinstance(adapter, Adapter)):
                        recovered = adapter.recover_foreground_pause(state.key, poll, control)
                        if recovered:
                            monitor.resume_from_pause(*recovered)
                            wait(poll)
                            continue
                    raise ManualRequired(monitor.reason)
                if action == 'start':
                    adapter.play(video)
                elif action == 'finished':
                    if pending_end is None:
                        pending_end = state.key
                        completed += 1
                        logging.info('视频自然结束；这不等于平台已确认课程完成。')
                        if control is not None:
                            control.emit('progress', current=state.current,
                                         duration=state.duration, completed=completed)
                    if task.max_chapters > 0 and completed >= task.max_chapters:
                        logging.info('达到本次视频数量上限 %s。', task.max_chapters)
                        return 'limit'
                    # 给平台处理自然结束事件的时间；仍未完成时转人工。
                    deadline = time.monotonic() + 30
                    while adapter.task_pending() and time.monotonic() < deadline:
                        reason = (adapter.guard(include_course=False) if activities is not None
                                  else adapter.guard())
                        if reason:
                            raise ManualRequired(reason)
                        if activities is not None and activities.identity():
                            break
                        wait(poll)
                    # 页面自身可能已经切换，避免再点一次而跳过新视频。
                    if activities is None or not activities.identity():
                        current = only_one(adapter.videos(), '可见视频')
                        if current is None or current.state.key == state.key:
                            adapter.advance(state.key)
                    pending_end = None
                    monitor = Monitor(stall_seconds=stall)
                    previous = previous_url = None
                wait(poll)
            except ManualRequired as exc:
                if previous is not None:
                    store.save(task.task_id, previous_url, previous, 'needs_user')
                choice = manual(str(exc))
                if choice != 'r':
                    return choice
                monitor = Monitor(stall_seconds=stall)
                missing_since = time.monotonic()
                previous = None
            except (EOFError, KeyboardInterrupt):
                return 'q'
            except Exception as exc:
                # 异常消息可能含签名 URL；日志只打印异常类型。
                if adapter.page.is_closed():
                    logging.warning('课程窗口已关闭，进度保留到最后一次观测。')
                    return 'q'
                choice = manual(f'页面读取或操作失败（{type(exc).__name__}），请检查网络和页面。')
                if choice != 'r':
                    return choice
                monitor = Monitor(stall_seconds=stall)
                missing_since = time.monotonic()
    finally:
        adapter.pause()


def select_page(context, platform):
    candidates = []
    for page in context.pages:
        if page.is_closed():
            continue
        try:
            validate_url(page.url, platform)
        except ValueError:
            continue
        candidates.append(page)
    if not candidates:
        raise ValueError('未找到对应平台页面，请在独立浏览器里打开课程。')
    if len(candidates) == 1:
        return candidates[0]
    for i, page in enumerate(candidates, 1):
        print(f'{i}. {page.title()}')
    while True:
        value = input('选择课程窗口编号：').strip()
        if value.isdigit() and 1 <= int(value) <= len(candidates):
            return candidates[int(value) - 1]


def make_parser():
    parser = argparse.ArgumentParser(description='网课播放辅助：超星 / 中国大学 MOOC / U校园 AI')
    parser.add_argument('command', choices=['inspect', 'run', 'status'])
    parser.add_argument('--platform', choices=list(START), default='chaoxing')
    parser.add_argument('--url', help='具体视频章节的网址；可省略并在浏览器中手动打开')
    parser.add_argument('--tasks', type=Path, help='课程任务 JSON；仅用于 run')
    parser.add_argument('--resume', action='store_true', help='恢复任务上次的章节网址')
    parser.add_argument('--task-id', help='单任务的固定 ID，用于恢复，例如 badminton')
    parser.add_argument('--channel', choices=['msedge', 'chrome', 'chromium'], default='msedge')
    parser.add_argument('--max-chapters', type=int, default=0, help='本次视频数量上限，0 表示不限')
    parser.add_argument('--poll', type=float, default=2)
    parser.add_argument('--stall', type=float, default=90)
    return parser


def main(argv=None):
    parser = make_parser()
    args = parser.parse_args(argv)
    if not 0 <= args.max_chapters <= 1000 or not 1 <= args.poll <= 10 or not 15 <= args.stall <= 600:
        parser.error('max-chapters: 0..1000（0 表示不限）；poll: 1..10 秒；stall: 15..600 秒。')
    if args.tasks and args.command != 'run':
        parser.error('--tasks 仅用于 run。')
    if args.tasks and (args.url or args.task_id):
        parser.error('--tasks 不与 --url 或 --task-id 同用。')
    tasks = load_tasks(args.tasks) if args.tasks else None
    if args.url:
        validate_url(args.url, args.platform)
    DATA.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(), logging.FileHandler(DATA / 'helper.log', encoding='utf-8')])
    store = Store(DATA / 'progress.sqlite3')
    try:
        if args.command == 'status':
            rows = store.rows()
            for row in rows:
                print(f"{row['task_id']} | {row['current']:.1f}/{row['duration']:.1f} 秒 | {row['status']}")
            if not rows:
                print('暂无本地记录。')
            return 0
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print('请先安装依赖：py -m pip install -r requirements.txt')
            return 2
        with sync_playwright() as playwright:
            options = {'headless': False, 'viewport': None}
            if args.channel != 'chromium':
                options['channel'] = args.channel
            try:
                context = playwright.chromium.launch_persistent_context(
                    str(DATA / f'profile-{args.channel}'), **options)
            except Exception:
                print('浏览器启动失败。请关闭本助手的其他实例；确认安装了 Edge，或按说明使用 Chromium。')
                return 2
            try:
                entries = tasks or [Task(args.task_id or '', args.platform, args.url or '', args.max_chapters)]
                for task in entries:
                    page = context.new_page()
                    url = store.resume_url(task.task_id) if args.resume and task.task_id else None
                    target = url or task.url or START[task.platform]
                    if not target:
                        raise ValueError('通用课程首次使用须提供课程链接。')
                    validate_url(target, task.platform)
                    page.goto(target, wait_until='domcontentloaded', timeout=60000)
                    print(f'\n平台：{task.platform}。请在独立浏览器手动登录并打开目标视频章节。')
                    input('确认页面后回到终端按 Enter：')
                    page = select_page(context, task.platform)
                    task.url = page.url
                    if not task.task_id:
                        task.task_id = hashlib.sha256(page_identity(task.url).encode()).hexdigest()[:12]
                        print(f'任务 ID：{task.task_id}；下次可用 --task-id {task.task_id} --resume')
                    adapter = Adapter(page, task.platform, task.next_selector, task.video_selector)
                    if args.command == 'inspect':
                        report = adapter.inspect()
                        path = DATA / 'inspect.json'
                        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                        print(json.dumps(report, ensure_ascii=False, indent=2))
                        print(f'检测报告：{path}')
                    else:
                        outcome = run_task(adapter, task, store, args.poll, args.stall)
                        if outcome == 'q':
                            break
                    if not page.is_closed():
                        page.close()
            finally:
                context.close()
    finally:
        store.close()
    return 0


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print('\n已退出。')
        raise SystemExit(130)
    except (ValueError, OSError) as exc:
        print(f'配置或文件错误：{exc}')
        raise SystemExit(2)
    except Exception as exc:
        print(f'运行中止（{type(exc).__name__}），请检查网络或重新登录后再运行。')
        raise SystemExit(2)
