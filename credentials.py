"""使用当前 Windows 用户的 DPAPI 保护本地 API Key；不记录凭据内容。"""

import base64
import binascii
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import os


_PREFIX = 'dpapi:v1:'
_MAGIC = b'course-helper:api-key:v1\0'
_MAX_KEY_BYTES = 16384
_MAX_BLOB_BYTES = 65536
_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD),
                ('pbData', ctypes.POINTER(wintypes.BYTE))]


def _crypt(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != 'nt':
        raise OSError('记住 API Key 需要 Windows 用户加密服务。')
    try:
        crypt32 = ctypes.WinDLL('crypt32.dll', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32.dll', use_last_error=True)
    except OSError:
        raise OSError('无法加载 Windows 用户加密服务。') from None

    blob_pointer = ctypes.POINTER(_DataBlob)
    function = (crypt32.CryptUnprotectData if decrypt
                else crypt32.CryptProtectData)
    description_type = (ctypes.POINTER(wintypes.LPWSTR) if decrypt
                        else wintypes.LPCWSTR)
    function.argtypes = [blob_pointer, description_type, blob_pointer,
                        ctypes.c_void_p, ctypes.c_void_p,
                        wintypes.DWORD, blob_pointer]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    input_buffer = ctypes.create_string_buffer(data, len(data))
    source = _DataBlob(len(data), ctypes.cast(
        input_buffer, ctypes.POINTER(wintypes.BYTE)))
    result = _DataBlob()
    try:
        # 不使用 LOCAL_MACHINE；NULL 描述指针避免额外的原生内存分配。
        if not function(ctypes.byref(source), None, None, None, None,
                        _UI_FORBIDDEN, ctypes.byref(result)):
            raise OSError(ctypes.get_last_error(), 'Windows 无法处理已保存的 API Key。')
        if not result.pbData or not 0 < result.cbData <= _MAX_BLOB_BYTES:
            raise ValueError('Windows 返回的密钥数据无效。')
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        # 清空 ctypes 可变缓冲区；Python 字符串本身不能保证原地擦除。
        ctypes.memset(input_buffer, 0, len(data))
        if result.pbData:
            if decrypt and result.cbData <= _MAX_BLOB_BYTES:
                ctypes.memset(result.pbData, 0, result.cbData)
            if kernel32.LocalFree(result.pbData):
                raise OSError('Windows 无法释放密钥处理缓冲区。')


def protect_api_key(value: str) -> str:
    """返回可写入配置文件的版本化密文；空值和无效输入抛出 ValueError。"""
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_KEY_BYTES:
        raise ValueError('API Key 为空、类型无效或过长。')
    try:
        raw = value.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('API Key 包含无效字符。') from None
    if len(raw) > _MAX_KEY_BYTES:
        raise ValueError('API Key 过长。')
    # 应用内校验补充 DPAPI 的完整性检查，拒绝损坏但仍能解密的内容。
    payload = _MAGIC + hashlib.sha256(raw).digest() + raw
    return _PREFIX + base64.b64encode(_crypt(payload, decrypt=False)).decode('ascii')


def unprotect_api_key(value: str) -> str:
    """还原本机当前用户保存的 Key；不将输入内容附加到错误消息。"""
    max_encoded_length = ((_MAX_BLOB_BYTES + 2) // 3) * 4
    if (not isinstance(value, str) or not value.startswith(_PREFIX)
            or len(value) > len(_PREFIX) + max_encoded_length):
        raise ValueError('已保存的 API Key 格式或版本无效。')
    encoded = value[len(_PREFIX):]
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('已保存的 API Key 编码无效。') from None
    if (not blob or len(blob) > _MAX_BLOB_BYTES
            or base64.b64encode(blob).decode('ascii') != encoded):
        raise ValueError('已保存的 API Key 编码无效。')

    payload = _crypt(blob, decrypt=True)
    offset = len(_MAGIC)
    raw = payload[offset + 32:]
    if (not payload.startswith(_MAGIC) or not 0 < len(raw) <= _MAX_KEY_BYTES
            or not hmac.compare_digest(payload[offset:offset + 32],
                                       hashlib.sha256(raw).digest())):
        raise ValueError('已保存的 API Key 内容损坏。')
    try:
        key = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('已保存的 API Key 内容无效。') from None
    if not key.strip():
        raise ValueError('已保存的 API Key 为空。')
    return key
