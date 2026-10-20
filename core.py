"""播放观察状态机与 SQLite 本地记录，不写入平台完成状态。"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit


@dataclass(frozen=True)
class VideoState:
    key: str
    current: float
    duration: float
    paused: bool
    ended: bool
    ready: int = 4
    error: int = 0


def validate_url(url: str, platform: str) -> str:
    """仅接受所选平台的 HTTPS 页面地址，返回未经改写的原地址。"""
    domains = {"chaoxing": "chaoxing.com", "mooc": "icourse163.org"}
    uai_hosts = {"ucloud.unipus.cn", "uai.unipus.cn", "sso.unipus.cn",
                 "ucontent.unipus.cn"}
    if platform not in domains and platform not in {"uai", "generic"}:
        raise ValueError("平台只能为 chaoxing、mooc、uai 或 generic")
    if not isinstance(url, str) or not url or any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 or char == "\\"
        for char in url
    ):
        raise ValueError("课程地址为空或包含无效字符")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as exc:
        raise ValueError("课程地址格式或端口无效") from exc
    if parsed.scheme != "https" or parsed.username is not None:
        raise ValueError("课程地址必须使用 HTTPS，且不得包含账号或密码")
    if port not in (None, 443) or parsed.netloc.endswith(":"):
        raise ValueError("课程地址仅允许默认 HTTPS 端口 443")
    labels_valid = len(host) <= 253 and all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in host.split(".")
    )
    # U校园入口会跳转到 ucontent 课件页；仅接受已确认的入口、课件和登录主机。
    if platform == "generic":
        host_allowed = True
    elif platform == "uai":
        host_allowed = host in uai_hosts
    else:
        domain = domains[platform]
        host_allowed = host == domain or host.endswith("." + domain)
    if not labels_valid or not host_allowed:
        raise ValueError("课程地址不属于所选平台的官方域名")
    return url


def detect_platform(url):
    """通用入口复用已有适配器；未知 HTTPS 站点使用通用规则。"""
    for platform in ('chaoxing', 'mooc', 'uai'):
        try:
            validate_url(url, platform)
            return platform
        except ValueError:
            pass
    validate_url(url, 'generic')
    return 'generic'


class Monitor:
    """返回下一步建议；manual 一经触发，仅新建实例可以恢复。

    now 应传入 time.monotonic()。仅初始暂停允许一次 start；播放结束
    需要看到正常速度的进度变化。finished 只代表本次播放器自然结束。
    """

    PAUSED_REASON = "视频已暂停或首次播放未成功，请手动处理"

    def __init__(self, stall_seconds: float = 90):
        if not math.isfinite(stall_seconds) or stall_seconds <= 0:
            raise ValueError("等待超时必须为有限的正数")
        self.stall_seconds = float(stall_seconds)
        self.reason = "等待播放器状态"
        self._locked = False
        self._key = None
        self._clock = None

    def _manual(self, reason: str) -> str:
        self._locked = True
        self.reason = reason
        return "manual"

    def resume_from_pause(self, state: VideoState, now: float) -> None:
        """焦点恢复后重新计时，保留暂停前已经观察到的正常进度。"""
        if (not self._locked or self.reason != self.PAUSED_REASON
                or not math.isfinite(now) or now < self._clock):
            raise ValueError("只能恢复已确认的普通暂停")
        if (state.key != self._key or state.ended or state.error
                or not math.isfinite(state.current) or not math.isfinite(state.duration)
                or state.current < 0 or state.current > state.duration + 0.75
                or abs(state.duration - self._duration) > max(1.0, self._duration * 0.01)
                or state.current < self._last_time - 0.25
                or state.current - self._last_time > (now - self._clock) * 1.05 + 0.75):
            raise ValueError("等待焦点期间视频进度异常，请确认页面")
        self._locked = False
        self._clock = self._last_sample = self._anchor_clock = self._progress_at = now
        self._anchor_time = self._last_time = state.current
        self._saw_playing = self._saw_playing or not state.paused

    def _begin(self, key: str, now: float) -> None:
        self._key = key
        self._last_time = None
        self._last_sample = None
        self._anchor_time = None
        self._anchor_clock = None
        self._duration = None
        self._progress_at = now
        self._progressed = False
        self._start_requested = False
        self._saw_playing = False
        self._finished = False

    def step(self, state: VideoState, now: float, blocked: bool = False) -> str:
        if self._locked:
            return "manual"
        if blocked:
            return self._manual("页面出现登录、验证或其他阻断，请手动处理")
        if state.error:
            return self._manual("播放器报告媒体错误，请检查页面")
        if not math.isfinite(now) or (self._clock is not None and now < self._clock):
            return self._manual("观察时钟无效或发生倒退，请重新启动辅助程序")
        if not state.key:
            return self._manual("无法识别当前视频，请检查页面")
        if state.key != self._key:
            self._begin(state.key, now)
        self._clock = now
        if self._finished:
            self.reason = "已报告本次自然播放结束，等待下一节"
            return "wait"
        if self._last_time is None and state.ended:
            return self._manual("首次观察视频已结束，请手动确认当前进度")
        if not math.isfinite(state.current) or state.current < 0:
            return self._manual("播放器时间无效，请检查页面")
        if math.isinf(state.duration):
            return self._manual("当前媒体可能为直播，无法自动判断自然结束")
        if math.isnan(state.duration) or state.duration <= 0:
            if state.ready < 1 and (math.isnan(state.duration) or state.duration == 0):
                if now - self._progress_at >= self.stall_seconds:
                    return self._manual("视频仍未加载，请在网页手动点击播放；开始播放后选择继续")
                if state.paused and not self._start_requested and not self._saw_playing:
                    # preload=none 的播放器需要先点击播放才会加载时长。
                    self._start_requested = True
                    self.reason = "正在尝试启动播放器并加载视频"
                    return "start"
                self._saw_playing = self._saw_playing or not state.paused
                self.reason = "等待视频元数据"
                return "wait"
            return self._manual("视频时长无效，无法自动判断自然结束")
        if state.current > state.duration + 0.75:
            return self._manual("播放器时间超出视频时长，请检查页面")
        if self._duration is not None and abs(state.duration - self._duration) > max(
            1.0, self._duration * 0.01
        ):
            return self._manual("同一视频的时长明显变化，请确认当前页面")
        self._duration = state.duration
        if self._anchor_time is None:
            self._anchor_time = state.current
            self._anchor_clock = now
        elif state.current - self._anchor_time > (now - self._anchor_clock) * 1.05 + 0.75:
            # 总进度也要与真实时间匹配，避免每次采样容差不断累积。
            return self._manual("累计播放进度超过正常速度，请手动确认进度")
        if self._last_time is not None:
            elapsed = now - self._last_sample
            if elapsed >= self.stall_seconds:
                return self._manual("播放观察中断过久，请确认当前进度")
            advance = state.current - self._last_time
            if advance < -0.25:
                return self._manual("检测到播放时间倒退，请手动确认进度")
            # 为采样与浏览器定时器留出少量容差，不把大幅跳时视为播放。
            if advance > elapsed * 1.25 + 0.75:
                return self._manual("检测到播放时间异常跳变，请手动确认进度")
            if advance > 0.01:
                self._progressed = True
                self._progress_at = now
        self._last_time = state.current
        self._last_sample = now
        if state.ended:
            if not self._progressed or not self._saw_playing:
                return self._manual("首次观察已结束或未观察到播放进度，请手动确认")
            if state.duration - state.current > 1.0:
                return self._manual("结束标记与播放进度不一致，请检查页面")
            self._finished = True
            self.reason = "已观察到本次视频自然播放结束；平台状态以页面为准"
            return "finished"
        if now - self._progress_at >= self.stall_seconds:
            return self._manual("播放进度长时间未前进，请检查网络或播放器")
        if (state.paused and not self._start_requested
                and not self._saw_playing and not self._progressed):
            # preload=metadata 也可能一直停在 readyState=1/2。
            self._start_requested = True
            self.reason = "当前视频尚未开始，允许尝试正常播放一次"
            return "start"
        if state.ready < 3:
            self._saw_playing = self._saw_playing or not state.paused
            self.reason = "播放器正在缓冲，等待恢复"
            return "wait"
        if state.paused:
            return self._manual(self.PAUSED_REASON)
        self._saw_playing = True
        self.reason = "正在观察正常播放进度"
        return "wait"


class Store:
    """每个任务和视频保留最近观察值；恢复仅返回页面 URL。"""

    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                task_id TEXT NOT NULL,
                video_key TEXT NOT NULL,
                url TEXT NOT NULL,
                snapshot TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL,
                PRIMARY KEY (task_id, video_key)
            )
        """)
        self._connection.commit()

    def save(self, task_id: str, url: str, state: VideoState, status: str):
        """只保存收到的观测字段，不补造播放时间或课程完成结果。"""
        if urlsplit(url).username is not None:
            raise ValueError("本地记录不接受包含账号或密码的 URL")
        # JSON 保留 NaN 等异常原值，避免 SQLite REAL 把 NaN 改为 NULL。
        snapshot = json.dumps(asdict(state), ensure_ascii=False)
        stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self._connection:
            self._connection.execute("""
                INSERT INTO observations
                    (task_id, video_key, url, snapshot, status, updated_at, revision)
                VALUES (?, ?, ?, ?, ?, ?,
                    (SELECT COALESCE(MAX(revision), 0) + 1 FROM observations))
                ON CONFLICT(task_id, video_key) DO UPDATE SET
                    url = excluded.url,
                    snapshot = excluded.snapshot,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    revision = excluded.revision
            """, (task_id, state.key, url, snapshot, status, stamp))

    def resume_url(self, task_id: str) -> str | None:
        row = self._connection.execute("""
            SELECT url FROM observations WHERE task_id = ?
            ORDER BY revision DESC LIMIT 1
        """, (task_id,)).fetchone()
        return row["url"] if row else None

    def rows(self) -> list[dict]:
        result = []
        for saved in self._connection.execute(
            "SELECT * FROM observations ORDER BY revision DESC"
        ):
            row = dict(saved)
            snapshot = json.loads(row.pop("snapshot"))
            row.update(snapshot)
            result.append(row)
        return result

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None
