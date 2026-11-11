"""Windows 本机 OCR；只接收题干/选项截图，不上传图像或读取账户信息。"""
import base64
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import time

from browser import ManualRequired
from ocr_diagnostics import save_failure


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
        $scale = [double]$env:COURSE_HELPER_OCR_SCALE
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
    readings = []
    try:
        return _read_image(png, readings)
    except ManualRequired as exc:
        save_failure(png, readings, exc)
        raise


def _read_image(png, readings):
    if os.name != 'nt':
        raise ManualRequired('特殊字体需要 Windows 本机文字识别，当前系统不支持，请手动处理。')
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / (
        'System32/WindowsPowerShell/v1.0/powershell.exe')
    deadline = time.monotonic() + 20
    try:
        if len(png) < 24 or not png.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('Invalid OCR image')
        width, height = struct.unpack('>II', png[16:24])
        expected = 2 if width < 100 and height < 80 else 1
        with tempfile.TemporaryDirectory(prefix='course-helper-ocr-') as directory:
            image = Path(directory) / 'question.png'
            image.write_bytes(png)
            # Retry a wholly empty short-text pass once at 2x size, within the
            # original 20-second deadline. A partial pass is not evidence to
            # combine across scales: it can repeat the same incorrect glyph.
            for scale in (1, 2):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('OCR deadline exceeded')
                env = dict(os.environ, COURSE_HELPER_OCR_IMAGE=str(image),
                           COURSE_HELPER_OCR_SCALE=str(scale))
                result = subprocess.run(
                    [str(powershell), '-NoProfile', '-NonInteractive', '-EncodedCommand',
                     base64.b64encode(_SCRIPT.encode('utf-16le')).decode('ascii')],
                    env=env, capture_output=True, timeout=remaining,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode:
                    raise ValueError('OCR unavailable')
                batch = [json.loads(line) for line in
                         result.stdout.decode('utf-8-sig').splitlines() if line.strip()]
                if len(batch) != expected or any(not isinstance(row, dict) or
                        not isinstance(row.get('text'), str) for row in batch):
                    raise ValueError('Invalid OCR response')
                # Windows OCR adds spaces between Chinese characters; retain
                # English word spacing and compare every nonempty reading.
                readings.extend(re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])', '',
                                       row['text'].strip()) for row in batch)
                valid = [text for text in readings if text]
                if any(len(text) > 20000 for text in valid):
                    raise ManualRequired('本机文字识别结果过长，请手动核对。')
                if len(set(valid)) > 1:
                    raise ManualRequired('单字或短选项的文字识别结果不一致，请手动核对；不会发送冲突的选项。')
                if len(valid) == expected:
                    return valid[0]
                if valid or expected == 1:
                    break
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ManualRequired('本机中文文字识别不可用或超时。请在 Windows 语言设置中安装简体中文文字识别后重试；不会发送乱码答案。') from None
    if not any(readings):
        raise ManualRequired('本机文字识别未识别出文字，请放大或展开题目后继续。')
    raise ManualRequired('有效文字识别结果不足，无法确认短选项，请手动核对。')


_VISIBLE_RECT = """(e, {inner=null,textOnly=false}) => {
    if (!inner && [e,...e.querySelectorAll('*')].some(n=>{
        const s=getComputedStyle(n);
        if (!n.getClientRects().length || !n.textContent.trim()) return false;
        return (/hidden|clip|scroll|auto/.test(s.overflowX) && n.scrollWidth>n.clientWidth+2) ||
               (/hidden|clip|scroll|auto/.test(s.overflowY) && n.scrollHeight>n.clientHeight+2);
    })) return null;
    const b=e.getBoundingClientRect();
    const sx=b.width/e.offsetWidth, sy=b.height/e.offsetHeight;
    const style=getComputedStyle(e);
    const ox=e.clientLeft+(parseFloat(style.paddingLeft)||0);
    const oy=e.clientTop+(parseFloat(style.paddingTop)||0);
    let r=inner ? {left:b.left+(ox+inner.left)*sx,
                  top:b.top+(oy+inner.top)*sy,
                  right:b.left+(ox+inner.right)*sx,
                  bottom:b.top+(oy+inner.bottom)*sy} :
                 {left:b.left,top:b.top,right:b.right,bottom:b.bottom};
    if (!inner && textOnly) {
        // A full-width paragraph can extend outside the viewport while all its
        // glyphs fit. Capture text ranges, not the paragraph's empty background.
        // Text ranges do not include generated conditions in ::before/::after.
        if ([e,...e.querySelectorAll('*')].some(n=>n.getClientRects().length &&
            ['::before','::after'].some(p=>
                !['none','normal','""',"''"].includes(getComputedStyle(n,p).content)))) return null;
        const walker=document.createTreeWalker(e,NodeFilter.SHOW_TEXT), boxes=[];
        while(walker.nextNode()) {
            const n=walker.currentNode, start=n.textContent.search(/\\S/);
            if(start<0) continue;
            const range=document.createRange();
            range.setStart(n,start); range.setEnd(n,n.textContent.trimEnd().length);
            boxes.push(...Array.from(range.getClientRects()).filter(b=>b.width && b.height));
        }
        if(!boxes.length) return null;
        r={left:Math.min(...boxes.map(b=>b.left)),top:Math.min(...boxes.map(b=>b.top)),
           right:Math.max(...boxes.map(b=>b.right)),bottom:Math.max(...boxes.map(b=>b.bottom))};
    }
    if (r.left < -1 || r.top < -1 ||
        r.right > innerWidth+1 || r.bottom > innerHeight+1) return null;
    const rootStyle=getComputedStyle(document.documentElement);
    for(let p=inner?e.parentElement:e;p;p=p.parentElement){
        // Root overflow applies to the viewport. Its box moves when scrolling;
        // using that box as another clipping region rejects visible questions.
        const s=getComputedStyle(p);
        // Containment on html/body prevents body overflow from propagating.
        if(p===document.documentElement || (p===document.body &&
            rootStyle.overflowX==='visible' && rootStyle.overflowY==='visible' &&
            rootStyle.contain==='none' && s.contain==='none')) continue;
        const b=p.getBoundingClientRect();
        const x=/hidden|clip|scroll|auto/.test(s.overflowX);
        const y=/hidden|clip|scroll|auto/.test(s.overflowY);
        if ((x && (r.left<b.left+p.clientLeft-1 ||
                   r.right>b.left+p.clientLeft+p.clientWidth+1)) ||
            (y && (r.top<b.top+p.clientTop-1 ||
                   r.bottom>b.top+p.clientTop+p.clientHeight+1))) return null;
    }
    return r;
}"""


def _check_visible_capture(node, scroll=True):
    """Screenshot rectangles must not include content clipped by a parent/frame."""
    if scroll:
        node.scroll_into_view_if_needed(timeout=5000)
    handle = node.element_handle()
    try:
        # Keep ordinary text layout, but crop wide containers around short
        # answers so blank space does not hide a single glyph from Windows OCR.
        for text_only in (False, True):
            rect = node.evaluate(_VISIBLE_RECT, {'textOnly': text_only})
            if rect and not text_only and rect['right']-rect['left'] >= 100:
                tight = node.evaluate(_VISIBLE_RECT, {'textOnly': True})
                if (tight and 2 <= tight['right']-tight['left'] < 100
                        and 2 <= tight['bottom']-tight['top'] < 80):
                    rect, text_only = tight, True
            source_rect = rect
            frame = handle.owner_frame()
            while rect and frame and frame.parent_frame:
                element = frame.frame_element()
                try:
                    rect = element.evaluate(_VISIBLE_RECT, {'inner': rect})
                finally:
                    element.dispose()
                frame = frame.parent_frame
            if rect:
                # offsetWidth rounds fractional iframe widths. Keep the frame
                # checks, but use Playwright's precise page coordinates for OCR.
                box = handle.bounding_box()
                if not box or not box['width'] or not box['height']:
                    rect = None
                    break
                if text_only:
                    local = handle.evaluate('''e=>{const b=e.getBoundingClientRect();
                        return {x:b.x,y:b.y,width:b.width,height:b.height}}''')
                    sx, sy = box['width']/local['width'], box['height']/local['height']
                    rect = {'left': box['x']+(source_rect['left']-local['x'])*sx,
                            'top': box['y']+(source_rect['top']-local['y'])*sy,
                            'right': box['x']+(source_rect['right']-local['x'])*sx,
                            'bottom': box['y']+(source_rect['bottom']-local['y'])*sy}
                else:
                    rect = {'left': box['x'], 'top': box['y'],
                            'right': box['x']+box['width'],
                            'bottom': box['y']+box['height']}
                break
    finally:
        handle.dispose()
    if rect is None:
        raise ManualRequired('特殊字体题干或选项被滚动区域裁剪，无法读取完整文字。请展开内容或调整窗口后继续。')
    return rect


def read_text(node):
    if not node.is_visible():
        raise ManualRequired('特殊字体题干或选项尚未显示，请等待加载后继续。')
    try:
        rect = _check_visible_capture(node)
        page = node.page
        origin = page.evaluate('({x:scrollX,y:scrollY})')
        left, top = math.floor(rect['left']), math.floor(rect['top'])
        width, height = math.ceil(rect['right'])-left, math.ceil(rect['bottom'])-top
        if not (2 <= width <= 2600 and 2 <= height <= 2600 and width*height <= 4_000_000):
            raise ManualRequired('特殊字体题目尺寸超出本机文字识别范围，请调整窗口后继续。')
        png = page.screenshot(type='png', animations='disabled', caret='hide', timeout=5000,
                              clip={'x': left, 'y': top,
                                    'width': width, 'height': height})
        after = _check_visible_capture(node, scroll=False)
        if (any(abs(after[key]-rect[key]) > .5 for key in rect)
                or origin != page.evaluate('({x:scrollX,y:scrollY})')):
            raise ManualRequired('截取题目时页面发生移动，请等待页面稳定后继续。')
    except ManualRequired:
        raise
    except Exception:
        raise ManualRequired('无法截取特殊字体题干或选项，请检查课程窗口后继续。') from None
    if len(png) > 4 * 1024 * 1024:
        raise ManualRequired('特殊字体题目截图过大，请手动处理。')
    return _recognize(png)
