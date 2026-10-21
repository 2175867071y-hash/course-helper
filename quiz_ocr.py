"""Windows 本机 OCR；只接收题干/选项截图，不上传图像或读取账户信息。"""
import base64
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from browser import ManualRequired


_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
Add-Type -AssemblyName System.Drawing
$null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]
$method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Await-Result($Operation, $ResultType) {
    $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $task.GetAwaiter().GetResult()
}
function Read-Text($fontRatio) {
# Small, tightly cropped inline text needs a white margin for Windows OCR.
$source = [System.Drawing.Image]::FromFile($env:COURSE_HELPER_OCR_IMAGE)
$paddedPath = $env:COURSE_HELPER_OCR_IMAGE + '.padded.png'
try {
    $scale = [Math]::Min(2, (2500.0 / [Math]::Max($source.Width, $source.Height)))
    $prefixWidth = 0
    if ($source.Width -lt 100 -and $source.Height -lt 80) {
        $prefixWidth = 180
        $scale = 1
    }
    $width = [int][Math]::Ceiling($source.Width * $scale)
    $height = [int][Math]::Ceiling($source.Height * $scale)
    if ($prefixWidth) {
        $fontSize = $height * $fontRatio
        $prefixWidth = [int][Math]::Ceiling($fontSize * 6) + 8
    }
    $padded = [System.Drawing.Bitmap]::new($width + 64 + $prefixWidth, [Math]::Max(100, $height + 64))
    try {
        $graphics = [System.Drawing.Graphics]::FromImage($padded)
        try {
            $graphics.Clear([System.Drawing.Color]::White)
            if ($prefixWidth) {
                # A fixed, local context helps detect isolated characters.
                # Bounding boxes below remove it from the recognized answer.
                $font = [System.Drawing.Font]::new('Microsoft YaHei', $fontSize,
                    [System.Drawing.FontStyle]::Regular, [System.Drawing.GraphicsUnit]::Pixel)
                try {
                    $graphics.DrawString('文字识别结果', $font, [System.Drawing.Brushes]::Black, 32, 32)
                } finally { $font.Dispose() }
            }
            $graphics.DrawImage($source, 32 + $prefixWidth, 32, $width, $height)
        } finally { $graphics.Dispose() }
        $padded.Save($paddedPath, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally { $padded.Dispose() }
} finally { $source.Dispose() }
$file = Await-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($paddedPath)) ([Windows.Storage.StorageFile])
$stream = Await-Result ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
try {
    $decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    try {
        $language = [Windows.Globalization.Language]::new('zh-Hans-CN')
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
        if ($null -eq $engine) { throw 'Missing Chinese OCR language' }
        $result = Await-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
        $text = $result.Text
        if ($prefixWidth) {
            $text = ($result.Lines | ForEach-Object { $_.Words } |
                Where-Object { $_.BoundingRect.Left -ge (31 + $prefixWidth) -and
                    $_.BoundingRect.Right -le (32 + $prefixWidth + $width + 3) } |
                ForEach-Object { $_.Text }) -join ' '
        }
        @{text=$text} | ConvertTo-Json -Compress
    } finally { $bitmap.Dispose() }
} finally { $stream.Dispose() }
}
$sizeProbe = [System.Drawing.Image]::FromFile($env:COURSE_HELPER_OCR_IMAGE)
try { $short = $sizeProbe.Width -lt 100 -and $sizeProbe.Height -lt 80 }
finally { $sizeProbe.Dispose() }
Read-Text 0.7
if ($short) { Read-Text 0.9 }
"""


@lru_cache(maxsize=32)
def _recognize(png):
    if os.name != 'nt':
        raise ManualRequired('特殊字体需要 Windows 本机文字识别，当前系统不支持，请手动处理。')
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / (
        'System32/WindowsPowerShell/v1.0/powershell.exe')
    try:
        with tempfile.TemporaryDirectory(prefix='course-helper-ocr-') as directory:
            image = Path(directory) / 'question.png'
            image.write_bytes(png)
            env = dict(os.environ, COURSE_HELPER_OCR_IMAGE=str(image))
            result = subprocess.run(
                [str(powershell), '-NoProfile', '-NonInteractive', '-EncodedCommand',
                 base64.b64encode(_SCRIPT.encode('utf-16le')).decode('ascii')],
                env=env, capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise ValueError('OCR unavailable')
        readings = [json.loads(line)['text'].strip() for line in
                    result.stdout.decode('utf-8-sig').splitlines() if line.strip()]
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ManualRequired('本机中文文字识别不可用或超时。请在 Windows 语言设置中安装简体中文文字识别后重试；不会发送乱码答案。') from None
    # Windows OCR 在每个汉字之间添加空格；保留英文词间空格。
    readings = [re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])', '', text)
                for text in readings]
    if len(readings) == 2 and (not readings[0] or readings[0] != readings[1]):
        raise ManualRequired('单字或短选项的两次文字识别结果不一致，请手动核对；不会发送不确定的选项。')
    text = readings[0] if readings else ''
    if not text or len(text) > 20000:
        raise ManualRequired('题干或选项的本机文字识别结果为空或过长，请手动核对。')
    return text


_VISIBLE_RECT = """(e, inner) => {
    if (!inner && [e,...e.querySelectorAll('*')].some(n=>{
        const s=getComputedStyle(n);
        if (!n.getClientRects().length || !n.textContent.trim()) return false;
        return (/hidden|clip|scroll|auto/.test(s.overflowX) && n.scrollWidth>n.clientWidth+2) ||
               (/hidden|clip|scroll|auto/.test(s.overflowY) && n.scrollHeight>n.clientHeight+2);
    })) return null;
    const b=e.getBoundingClientRect();
    const sx=b.width/e.offsetWidth, sy=b.height/e.offsetHeight;
    let r=inner ? {left:b.left+(e.clientLeft+inner.left)*sx,
                  top:b.top+(e.clientTop+inner.top)*sy,
                  right:b.left+(e.clientLeft+inner.right)*sx,
                  bottom:b.top+(e.clientTop+inner.bottom)*sy} :
                 {left:b.left,top:b.top,right:b.right,bottom:b.bottom};
    if (r.left < -1 || r.top < -1 ||
        r.right > innerWidth+1 || r.bottom > innerHeight+1) return null;
    for(let p=e.parentElement;p;p=p.parentElement){
        const s=getComputedStyle(p), b=p.getBoundingClientRect();
        const x=/hidden|clip|scroll|auto/.test(s.overflowX);
        const y=/hidden|clip|scroll|auto/.test(s.overflowY);
        if ((x && (r.left<b.left+p.clientLeft-1 ||
                   r.right>b.left+p.clientLeft+p.clientWidth+1)) ||
            (y && (r.top<b.top+p.clientTop-1 ||
                   r.bottom>b.top+p.clientTop+p.clientHeight+1))) return null;
    }
    return r;
}"""


def _check_visible_capture(node):
    """Screenshot rectangles must not include content clipped by a parent/frame."""
    node.scroll_into_view_if_needed(timeout=5000)
    rect = node.evaluate(_VISIBLE_RECT, None)
    handle = node.element_handle()
    frame = handle.owner_frame()
    while rect and frame and frame.parent_frame:
        element = frame.frame_element()
        try:
            rect = element.evaluate(_VISIBLE_RECT, rect)
        finally:
            element.dispose()
        frame = frame.parent_frame
    handle.dispose()
    if rect is None:
        raise ManualRequired('特殊字体题干或选项被滚动区域裁剪，无法读取完整文字。请展开内容或调整窗口后继续。')


def read_text(node):
    if not node.is_visible():
        raise ManualRequired('特殊字体题干或选项尚未显示，请等待加载后继续。')
    box = node.bounding_box()
    if (not box or not 2 <= box['width'] <= 2600 or not 2 <= box['height'] <= 2600
            or box['width'] * box['height'] > 4_000_000):
        raise ManualRequired('特殊字体题目尺寸超出本机文字识别范围，请调整窗口后继续。')
    try:
        _check_visible_capture(node)
        png = node.screenshot(type='png', animations='disabled', caret='hide', timeout=5000)
        _check_visible_capture(node)
    except ManualRequired:
        raise
    except Exception:
        raise ManualRequired('无法截取特殊字体题干或选项，请检查课程窗口后继续。') from None
    if len(png) > 4 * 1024 * 1024:
        raise ManualRequired('特殊字体题目截图过大，请手动处理。')
    return _recognize(png)
