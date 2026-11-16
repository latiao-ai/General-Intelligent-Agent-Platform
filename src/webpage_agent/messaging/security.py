"""签名、加解密、脱敏与 SSRF 防护。

这些函数全部是**纯函数**（除了 `assert_public_http_url` 只做本地判断），所以能
用固定向量写单测。企微的 AES 向量直接取自官方文档示例；飞书的实现按官方
"事件订阅加密"规则：`sha256(encrypt_key)` 派生 AES-256 key，密文前 16 字节是 IV。

**不要把凭据写进日志**：`mask_secret` 只保留前 4 位，`chat_id_hash` 只给排障用。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import struct
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)

MAX_CALLBACK_BYTES = 1_000_000


# ---------------------------------------------------------------- 脱敏


def mask_secret(value: str) -> str:
    """展示用掩码：只留前 4 位与末 2 位，短值全掩。"""
    text = str(value or '')
    if not text:
        return ''
    if len(text) <= 8:
        return '•' * 8
    return f'{text[:4]}••••{text[-2:]}'


def chat_id_hash(value: str) -> str:
    """日志/审计里的会话标识：不可逆、定长。"""
    return hashlib.sha256(str(value or '').encode('utf-8')).hexdigest()[:12]


# ---------------------------------------------------------------- 飞书


def feishu_signature(timestamp: str, nonce: str, encrypt_key: str,
                     body: bytes | str) -> str:
    """`X-Lark-Signature`：sha256(timestamp + nonce + encrypt_key + body)。

    body 必须是**原始请求体字节**（不是重新序列化的 JSON），否则签名必错。
    """
    raw = body if isinstance(body, bytes) else str(body).encode('utf-8')
    material = (str(timestamp) + str(nonce) + str(encrypt_key)).encode('utf-8') + raw
    return hashlib.sha256(material).hexdigest()


def verify_feishu_signature(headers, body: bytes, encrypt_key: str) -> bool:
    if not encrypt_key:
        return False
    timestamp = str(headers.get('x-lark-request-timestamp') or '')
    nonce = str(headers.get('x-lark-request-nonce') or '')
    signature = str(headers.get('x-lark-signature') or '')
    if not timestamp or not nonce or not signature:
        return False
    expected = feishu_signature(timestamp, nonce, encrypt_key, body)
    return hmac.compare_digest(expected, signature)


def _feishu_key(encrypt_key: str) -> bytes:
    return hashlib.sha256(encrypt_key.encode('utf-8')).digest()


def feishu_decrypt(encrypt_b64: str, encrypt_key: str) -> str:
    """解密 `{"encrypt": "..."}` 里的密文，返回明文 JSON 字符串。"""
    key = _feishu_key(encrypt_key)
    raw = base64.b64decode(encrypt_b64)
    if len(raw) <= 16:
        raise ValueError('飞书密文长度不足')
    iv, ciphertext = raw[:16], raw[16:]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    pad = padded[-1] if padded else 0
    if not 1 <= pad <= 16 or padded[-pad:] != bytes([pad]) * pad:
        raise ValueError('飞书密文填充不合法')
    return padded[:-pad].decode('utf-8')


def feishu_encrypt(plaintext: str, encrypt_key: str) -> str:
    """测试与本地联调用；生产不加密（平台侧加密）。"""
    import os
    key = _feishu_key(encrypt_key)
    iv = os.urandom(16)
    data = str(plaintext).encode('utf-8')
    pad = 16 - (len(data) % 16)
    padded = data + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(iv + ciphertext).decode('ascii')


# ---------------------------------------------------------------- 企业微信


def wecom_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """企微回调签名：四参数字典序拼接后 sha1。"""
    parts = sorted([str(token), str(timestamp), str(nonce), str(encrypt)])
    return hashlib.sha1(''.join(parts).encode('utf-8')).hexdigest()


def verify_wecom_signature(*, token: str, timestamp: str, nonce: str,
                           encrypt: str, signature: str) -> bool:
    if not token:
        return False
    expected = wecom_signature(token, timestamp, nonce, encrypt)
    return hmac.compare_digest(expected, str(signature or ''))


def _wecom_key(aes_key_b64: str) -> bytes:
    # 官方 EncodingAESKey 是 43 位（不带 =）；这里也容忍手填时带了尾部 =。
    text = str(aes_key_b64).strip().rstrip('=')
    key = base64.b64decode(text + '=')
    if len(key) != 32:
        raise ValueError(f'EncodingAESKey 长度不合法（{len(key)} 字节）')
    return key


def _wecom_decrypt_raw(ciphertext: bytes, key: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    pad = padded[-1] if padded else 0
    if not 1 <= pad <= 32 or padded[-pad:] != bytes([pad]) * pad:
        raise ValueError('企微密文填充不合法')
    return padded[:-pad]


def wecom_decrypt(encrypt_b64: str, aes_key_b64: str) -> tuple[str, str]:
    """解密企微回调密文，返回 `(消息体, receive_id)`。

    消息体可能是 XML（普通消息）或纯文本（GET 验证的 echostr）。
    """
    key = _wecom_key(aes_key_b64)
    plain = _wecom_decrypt_raw(base64.b64decode(encrypt_b64), key)
    if len(plain) < 20:
        raise ValueError('企微明文长度不足')
    msg_len = struct.unpack('>I', plain[16:20])[0]
    message = plain[20:20 + msg_len].decode('utf-8')
    receive_id = plain[20 + msg_len:].decode('utf-8', errors='ignore')
    return message, receive_id


def wecom_encrypt(message: str, aes_key_b64: str, receive_id: str = '') -> str:
    """加密回调报文（测试与被动回复用）。"""
    import os
    key = _wecom_key(aes_key_b64)
    body = str(message).encode('utf-8')
    plain = os.urandom(16) + struct.pack('>I', len(body)) + body + str(receive_id).encode('utf-8')
    pad = 32 - (len(plain) % 32)
    padded = plain + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode('ascii')


def parse_xml(text: str) -> ET.Element:
    """安全解析回调 XML。

    `xml.etree` 默认不解析外部实体，但仍要防实体膨胀：限制大小、直接拒绝
    DOCTYPE/ENTITY 声明。回调报文只有几百字节，正常消息不会带这些。
    """
    raw = str(text or '')
    if len(raw.encode('utf-8')) > MAX_CALLBACK_BYTES:
        raise ValueError('回调报文过大')
    lowered = raw.lower()
    if '<!doctype' in lowered or '<!entity' in lowered:
        raise ValueError('回调报文包含不允许的 DTD 声明')
    return ET.fromstring(raw)


def xml_text(root: ET.Element, tag: str) -> str:
    node = root.find(tag)
    if node is None or node.text is None:
        return ''
    return node.text.strip()


# ---------------------------------------------------------------- SSRF


_BLOCKED_HOSTS = {'localhost', 'localhost.localdomain', 'metadata.google.internal'}


def assert_public_http_url(url: str) -> str:
    """媒体下载只允许公网 http/https。

    只判断字面量 IP 与保留域名；**不做 DNS 解析**。真正的重绑定防护要靠出口代理
    或连接后校验对端 IP，这一版不假装做到了——不能解析域名时宁可拒绝未知协议。
    """
    text = str(url or '').strip()
    if not text:
        raise ValueError('地址为空')
    parts = urlsplit(text)
    if parts.scheme not in ('http', 'https'):
        raise ValueError('只允许 http/https')
    host = (parts.hostname or '').lower()
    if not host or host in _BLOCKED_HOSTS or host.endswith('.local'):
        raise ValueError('不允许的主机名')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return text
    if (address.is_private or address.is_loopback or address.is_link_local
            or address.is_reserved or address.is_multicast):
        raise ValueError('不允许访问内网地址')
    return text
