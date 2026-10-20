"""将题目音频发送到用户单独配置的兼容语音转写接口，不复用其他服务的密钥。"""
import json
import urllib.request
import uuid

from quiz_ai import QuizAIError, _endpoint, _NoRedirect, _text


def transcribe(sound, base_url, model, api_key):
    endpoint = _endpoint(base_url).removesuffix('/chat/completions')
    if not endpoint.endswith('/audio/transcriptions'):
        endpoint += '/audio/transcriptions'
    if (not _text(model, 200) or not api_key or
            any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in api_key)):
        raise QuizAIError('语音转写模型或密钥格式无效')
    boundary = 'CourseHelper' + uuid.uuid4().hex
    fields = []
    for name, value in (('model', model), ('response_format', 'json')):
        fields.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                       f'{value}\r\n').encode())
    fields.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="listening.{sound.format}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
        + sound.data + f'\r\n--{boundary}--\r\n'.encode())
    try:
        request = urllib.request.Request(endpoint, data=b''.join(fields), headers={
            'Authorization': 'Bearer ' + api_key, 'Accept': 'application/json',
            'Content-Type': 'multipart/form-data; boundary=' + boundary}, method='POST')
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=30) as response:
            if not 200 <= response.getcode() < 300:
                raise QuizAIError('语音转写服务未成功，请检查接口、模型及额度')
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise QuizAIError('语音转写结果过长，不会截断听力材料')
        result = json.loads(raw.decode('utf-8-sig'))
        transcript = result.get('text') if isinstance(result, dict) else None
        if not _text(transcript, 50000):
            raise QuizAIError('语音转写结果为空、无效或过长，不能只凭题干猜答案')
        return transcript.strip()
    except QuizAIError:
        raise
    except Exception:
        raise QuizAIError('语音转写失败或超时，请检查独立转写服务配置；不会发送到文字接口猜答案') from None
