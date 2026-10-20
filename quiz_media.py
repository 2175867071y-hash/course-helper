"""只采集当前题组中的可见图片与公开给播放器的完整音频。"""
import base64
import hashlib
from browser import ManualRequired
from quiz_ai import QuizImage, QuizAudio


IMAGE_SELECTOR = 'img, canvas, svg'
MEDIA_SELECTOR = 'img, canvas, svg, audio, video, object, embed'
EXCLUDED = 'aside,nav,header,footer,[role=search],button:not([role=radio]):not([role=checkbox]),[aria-hidden=true]'


def media_nodes(scope):
    nodes = []
    for node in scope.locator(MEDIA_SELECTOR).all():
        if node.evaluate('(e,s)=>!!e.closest(s)', EXCLUDED):
            continue
        tag = node.evaluate('e=>e.tagName.toLowerCase()')
        if not node.is_visible() and tag != 'audio':
            continue
        # SVG 的子节点不是第二张图片；不读取与题目无关的浏览器工具栏。
        nodes.append(node)
    return nodes


def image_bytes(node):
    if node.evaluate("e=>e.tagName==='IMG' && (!e.complete || !e.naturalWidth)"):
        raise ManualRequired('题目图片尚未加载完整，请等待图片加载后继续。')
    box = node.bounding_box()
    if (not box or not 1 <= box['width'] <= 4096 or not 1 <= box['height'] <= 4096
            or box['width'] * box['height'] > 8_000_000):
        raise ManualRequired('题目图片尺寸过大或不可见，不能截断图片后答题。')
    try:
        # 原始位图/画布与独立 SVG 按固定原点渲染，避免拖动后的亚像素位置改变截图。
        # 跨域污染或复杂 SVG 无法导出时仍使用可见截图，复核不一致则暂停。
        encoded = node.evaluate("""async e => {
            try {
                if (e.tagName === 'CANVAS') return e.toDataURL('image/png').split(',')[1];
                const box=e.getBoundingClientRect(), c=document.createElement('canvas');
                c.width=Math.ceil(box.width); c.height=Math.ceil(box.height);
                let source=e;
                if (e.tagName.toLowerCase() === 'svg') {
                    if(e.querySelector('[href], [*|href], foreignObject, image')) return null;
                    const copy=e.cloneNode(true), originals=[e,...e.querySelectorAll('*')];
                    [copy,...copy.querySelectorAll('*')].forEach((n,i)=>{
                        const style=getComputedStyle(originals[i]);
                        n.setAttribute('style',[...style].map(k=>k+':'+style.getPropertyValue(k)).join(';'));
                    });
                    copy.setAttribute('width',String(c.width));copy.setAttribute('height',String(c.height));
                    const url=URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(copy)],{type:'image/svg+xml'}));
                    source=new Image();
                    try {
                        await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(),2000);
                            source.onload=()=>{clearTimeout(timer);resolve();};
                            source.onerror=()=>{clearTimeout(timer);reject();};source.src=url;});
                    } finally {URL.revokeObjectURL(url);}
                }
                c.getContext('2d').drawImage(source,0,0,c.width,c.height);
                return c.toDataURL('image/png').split(',')[1];
            } catch (_) {return null;}
        }""")
        content = (base64.b64decode(encoded, validate=True) if encoded else
                   node.screenshot(type='png', animations='disabled', caret='hide', timeout=5000))
    except Exception:
        raise ManualRequired('无法完整截取题目图片，请检查图片是否被遮挡或仍在加载。') from None
    if len(content) > 4 * 1024 * 1024:
        raise ManualRequired('单张题目图片超过 4 MiB，不能截断后答题。')
    return content


def audio_bytes(node):
    # Fetch 仅使用 DOM 中播放器公开的地址；不读取账户、Cookie 或内部平台接口。
    result = node.evaluate("""async e => {
        const src=e.currentSrc || e.src || e.querySelector('source')?.src;
        if (!src || !/^(https?:|blob:|data:audio\\/)/i.test(src)) return {error:'source'};
        if (/\\.m3u8(?:[?#]|$)|\\.mpd(?:[?#]|$)/i.test(src)) return {error:'stream'};
        const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),20000);
        try {
            const response=await fetch(src,{credentials:'same-origin',redirect:'error',signal:controller.signal});
            if (!response.ok || !response.body) return {error:'fetch'};
            const max=20*1024*1024;
            if (Number(response.headers.get('content-length'))>max) return {error:'size'};
            const reader=response.body.getReader(), chunks=[]; let size=0;
            while (true) {
                const {done,value}=await reader.read(); if (done) break;
                size+=value.byteLength;
                if (size>max) {await reader.cancel(); return {error:'size'};}
                chunks.push(value);
            }
            const blob=new Blob(chunks,{type:response.headers.get('content-type') || ''});
            const data=await new Promise((resolve,reject)=>{const reader=new FileReader();
                reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsDataURL(blob);});
            return {data:String(data).split(',')[1]};
        } catch (_) {return {error:'fetch'};} finally {clearTimeout(timer);controller.abort();}
    }""")
    if 'error' in result:
        messages = {'size': '听力音频超过 20 MiB，暂不支持自动分段转写。',
                    'stream': '当前听力使用分片流，尚不能取得完整音频。'}
        raise ManualRequired(messages.get(result['error'],
            '无法读取完整听力音频（可能未加载、跨域受限或没有可读取的音频地址）。'))
    try:
        data = base64.b64decode(result['data'], validate=True)
    except Exception:
        raise ManualRequired('听力音频读取失败。') from None
    if data.startswith(b'RIFF') and data[8:12] == b'WAVE':
        fmt = 'wav'
    elif data.startswith(b'ID3') or (len(data) > 1 and data[0] == 255 and data[1] & 0xE0 == 0xE0):
        fmt = 'mp3'
    elif data[4:8] == b'ftyp':
        fmt = 'mp4'
    elif data.startswith(b'OggS'):
        fmt = 'ogg'
    elif data.startswith(b'\x1aE\xdf\xa3'):
        fmt = 'webm'
    elif data.startswith(b'fLaC'):
        fmt = 'flac'
    else:
        raise ManualRequired('听力地址返回的内容不是支持的完整音频文件。')
    if not data or len(data) > 20 * 1024 * 1024:
        raise ManualRequired('听力音频为空或过大。')
    return data, fmt


def collect_media(item, scope, enabled):
    """返回各题的图片/音频及元素对应关系；诊断调用不进入本函数。"""
    by_question = [([], []) for _ in item.root]
    mapping = []
    total_images = total_audio = 0
    nodes = media_nodes(scope)
    if len(nodes) > 28:
        raise ManualRequired('题组媒体数量过多，请分开处理。')
    for index, node in enumerate(nodes):
        if not enabled:
            raise ManualRequired('小测含图片或音频媒体，请配置图片模型或语音转写服务后重新开始。')
        tag = node.evaluate('e=>e.tagName.toLowerCase()')
        owners = [i for i, group in enumerate(item.root) if
                  group.evaluate('(e,child)=>e.contains(child)', node.element_handle())]
        if not owners:
            # main 也可能有头像和账户卡；共享媒体必须具有明确材料语义。
            if not node.evaluate(
                "e=>!!e.closest('[data-passage],.passage,.reading-passage,.material-content,.directions,.instructions,.abs-direction')"):
                raise ManualRequired('图片或音频不在明确题目材料区域，无法确认其对应题目。')
            owners = list(range(len(item.root)))
        if tag in ('img', 'canvas', 'svg'):
            data = image_bytes(node)
            identity = 'image_' + hashlib.sha256(data).hexdigest()[:24]
            media = QuizImage(identity, data)
            total_images += len(media.data)
            if total_images > 12 * 1024 * 1024:
                raise ManualRequired('整题组图片超过 12 MiB，不会截断图片。')
            for owner in owners:
                by_question[owner][0].append(media)
        elif tag == 'audio':
            data, fmt = audio_bytes(node)
            identity = 'audio_' + hashlib.sha256(data).hexdigest()[:24]
            media = QuizAudio(identity, data, fmt)
            total_audio += len(data)
            if total_audio > 40 * 1024 * 1024:
                raise ManualRequired('整题组音频超过 40 MiB，不会截断音频。')
            for owner in owners:
                by_question[owner][1].append(media)
        else:
            raise ManualRequired('题目含视频、插件或嵌入媒体，当前图片/音频处理尚不支持此结构。')
        mapping.append((node, identity))
    # 移动图片选项会改变 DOM 顺序，材料身份依赖内容；移动不能改变题目快照。
    for images, audio in by_question:
        images[:] = sorted({m.id: m for m in images}.values(), key=lambda m: m.id)
        audio[:] = sorted({m.id: m for m in audio}.values(), key=lambda m: m.id)
    return by_question, mapping


def references(node, mapping, exclude_sources=False):
    return [identity for media, identity in mapping if
            node.evaluate('(e,m)=>e===m || e.contains(m)', media.element_handle()) and
            (not exclude_sources or not media.evaluate(
                "e=>!!e.closest('[draggable=true],[data-drag-id],.drag-item')"))]
