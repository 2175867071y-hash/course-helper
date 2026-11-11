"""每个课程仅保留最近一次 OCR 失败，内容限定为输入图片与识别结果。"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import struct
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED


_directory = ContextVar('ocr_failure_directory', default=None)


@contextmanager
def record_failures(directory):
    token = _directory.set(Path(directory))
    try:
        yield
    finally:
        _directory.reset(token)


def save_failure(png, readings, error):
    directory = _directory.get()
    if directory is None:
        return
    # The caller only passes a cropped question region, never a full page,
    # browser URL, profile, settings, or API credentials.
    if (len(png) < 24 or len(png) > 4 * 1024 * 1024
            or not png.startswith(b'\x89PNG\r\n\x1a\n')):
        return
    temporary = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        report = {
            'format_version': 1,
            'captured_at': datetime.now(timezone.utc).isoformat(),
            'image_size': list(struct.unpack('>II', png[16:24])),
            'error': str(error)[:512],
            'readings': [value[:256] for value in readings[:4]],
            'readings_truncated': len(readings) > 4 or any(len(value) > 256 for value in readings),
        }
        # Replace a single archive atomically so image and report cannot belong
        # to different failures; a failed write preserves the previous archive.
        with tempfile.NamedTemporaryFile(dir=directory, suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
        with ZipFile(temporary, 'w', ZIP_DEFLATED) as archive:
            archive.writestr('region.png', png)
            archive.writestr('report.json', json.dumps(report, ensure_ascii=False, indent=2))
        destination = directory / 'ocr-failure.zip'
        os.replace(temporary, destination)
        logging.warning('文字识别失败，截图尺寸 %s，有效读取 %s / %s。诊断已保存：%s',
                        report['image_size'], sum(bool(text) for text in readings), len(readings), destination)
    except OSError:
        logging.warning('本地识别诊断保存失败；原识别错误仍有效，请检查磁盘空间或文件占用。')
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
