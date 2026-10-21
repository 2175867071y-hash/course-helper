"""页面切换期间仅重试只读操作；异常诊断不含页面地址或响应原文。"""
from functools import wraps
from pathlib import Path
import time


def is_loading_error(exc):
    from playwright.sync_api import Error
    return isinstance(exc, Error) and any(text in str(exc).lower() for text in (
        'execution context was destroyed', 'frame was detached', 'frame has been detached',
        'cannot find context with specified id', 'unable to adopt element handle',
        'frame does not have execution context'))


def retry_dom_read(function):
    @wraps(function)
    def read(*args, **kwargs):
        for attempt in range(3):
            try:
                return function(*args, **kwargs)
            except Exception as exc:
                if not is_loading_error(exc):
                    raise
                if attempt == 2:
                    from browser import ManualRequired
                    raise ManualRequired('课程页面仍在加载或切换，请稍后选择继续。未重复点击或提交。') from None
                owner = args[0] if args else None
                control = getattr(owner, 'control', None)
                if control is None:
                    control = getattr(getattr(owner, 'adapter', None), 'control', None)
                if control is not None:
                    control.check()
                    control.sleep(0.25 * (attempt + 1))
                else:
                    time.sleep(0.25 * (attempt + 1))
    return read


def safe_error_location(exc):
    root = Path(__file__).resolve().parent
    trace = exc.__traceback__
    location = ''
    while trace:
        code = trace.tb_frame.f_code
        path = Path(code.co_filename).resolve()
        if path.parent == root:
            location = f'{path.name}:{trace.tb_lineno} {code.co_name}'
        trace = trace.tb_next
    return type(exc).__name__ + (f'；{location}' if location else '')
