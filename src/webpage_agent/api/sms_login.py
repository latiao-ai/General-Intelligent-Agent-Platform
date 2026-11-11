"""短信验证码登录的运行期：滑块 → 发码 → 校验 → 建中台会话。

与 `platform_login.py`（账号密码）并列，是第三种登录方式。差异只在"谁证明身份"：

- 账号密码登录：密码交给**子平台自己的接口**校验；
- 短信登录：手机号验证码交给**内部短信平台**校验（`integrations/sms_client.py`）。

身份落库形态与其它登录方式**完全一致**：方案 A 下，短信登录是独立身份源
（`identity_provider_id = sms`），`subject = 手机号`。首次登录自动建号，之后复用
同一个中台账号。会话仍走 `login_platform_identity`，密码/验证码不进本库。

本模块只做业务编排，不直接发 HTTP：发码/校验都经 `sms_client`，由它负责签名。
"""
import base64
import json
import logging
import re

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as sym_padding

from webpage_agent.core.config import settings
from webpage_agent.integrations import sms_client

logger = logging.getLogger(__name__)

# 中国大陆手机号：1 开头，第二位 3-9，共 11 位。
_MOBILE = re.compile(r'^1[3-9]\d{9}$')


def encrypt_point(secret_key: str, x: int, y: int = 5) -> str:
    """按 aj-captcha 协议加密坐标：AES-ECB + PKCS7 + Base64。

    明文是 `{"x":<x>,"y":<y>}` 的 JSON 字符串，密钥直接用 secretKey（16 字节）。
    返回 Base64 密文，作为 check 接口的 pointJson。

    浏览器原生 Web Crypto 不支持 ECB，故加密放在后端用 cryptography 完成，
    前端只把拖动距离 x 透传过来。
    """
    plaintext = json.dumps({'x': int(x), 'y': int(y)},
                           separators=(',', ':')).encode('utf-8')
    key = secret_key.encode('utf-8')[:16].ljust(16, b'\x00')
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode('ascii')


class SmsLoginError(RuntimeError):
    """短信登录链路上的失败。`status` 直接当 HTTP 状态码用。"""

    def __init__(self, detail: str, status: int = 400) -> None:
        self.status = status
        self.detail = detail
        super().__init__(detail)


def is_configured() -> bool:
    """凭据与服务地址齐全才算可用；功能总开关另看 `settings.sms_login_enabled`。"""
    return bool(settings.sms_base_url and settings.sms_app_key
                and settings.sms_app_secret)


def normalize_mobile(raw: str) -> str:
    """校验并返回规范化手机号。不合法抛 SmsLoginError。"""
    mobile = (raw or '').strip()
    if not _MOBILE.match(mobile):
        raise SmsLoginError('手机号格式不正确', status=400)
    return mobile


def _image_mime(b64: str) -> str:
    """按 base64 魔数判断图片 MIME 类型。平台背景图是 JPEG，拼图块是 PNG。"""
    raw = base64.b64decode(b64 or '')
    if raw[:2] == b'\xff\xd8':
        return 'image/jpeg'
    if raw[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if raw[:4] == b'GIF8':
        return 'image/gif'
    return 'image/png'


async def get_captcha(user_ip: str) -> dict:
    """获取滑块。返回给前端的纯 Base64 图片 + 格式 + token + secretKey。"""
    if not is_configured():
        raise SmsLoginError('短信登录未启用', status=404)
    if not user_ip:
        raise SmsLoginError('无法解析用户地址', status=400)
    body = await sms_client.get_captcha(user_ip)
    code = body.get('code')
    if code not in (0, '0'):
        # 鉴权失败 / 参数错误 / 频控等，原样保留 msg 供前端展示。
        raise SmsLoginError(str(body.get('msg') or '获取滑块失败'),
                            status=_http_status_for_code(code))
    data = body.get('data') or {}
    if data.get('repCode') != '0000':
        raise SmsLoginError(str(data.get('repMsg') or '获取滑块失败'), status=400)
    rep_data = data.get('repData') or {}
    bg_b64 = str(rep_data.get('originalImageBase64') or '')
    jig_b64 = str(rep_data.get('jigsawImageBase64') or '')
    return {
        'captchaType': rep_data.get('captchaType') or 'blockPuzzle',
        'originalImageBase64': bg_b64,
        'originalImageType': _image_mime(bg_b64),
        'jigsawImageBase64': jig_b64,
        'jigsawImageType': _image_mime(jig_b64),
        'secretKey': rep_data.get('secretKey', ''),
        'token': rep_data.get('token', ''),
    }


async def check_captcha(token: str, secret_key: str, x: int, mobile: str,
                        user_ip: str) -> str:
    """校验滑块，返回服务端票据 captchaVerification。

    坐标加密（AES-ECB）在后端完成，前端只传拖动距离 x 与 secretKey。
    """
    mobile = normalize_mobile(mobile)
    point_json = encrypt_point(secret_key, x, y=5)
    body = await sms_client.check_captcha(token, point_json, mobile, user_ip)
    code = body.get('code')
    if code not in (0, '0'):
        raise SmsLoginError(str(body.get('msg') or '滑块校验失败'),
                            status=_http_status_for_code(code))
    data = body.get('data') or {}
    if data.get('repCode') != '0000':
        raise SmsLoginError(str(data.get('repMsg') or '滑块校验失败'), status=400)
    ticket = str(data.get('captchaVerification') or '').strip()
    if not ticket:
        raise SmsLoginError('未取得验证票据', status=502)
    return ticket


async def send_code(mobile: str, captcha_verification: str,
                    user_ip: str) -> dict:
    """发送短信验证码，返回 serialNo / 有效期 / 重发间隔。"""
    mobile = normalize_mobile(mobile)
    body = await sms_client.send_code(mobile, captcha_verification, user_ip)
    code = body.get('code')
    if code not in (0, '0'):
        # 429 频控、400 票据错误等，原样 msg。
        raise SmsLoginError(str(body.get('msg') or '发送失败'),
                            status=_http_status_for_code(code))
    data = body.get('data') or {}
    serial_no = str(data.get('serialNo') or '')
    if not serial_no:
        raise SmsLoginError('未取得发送流水号', status=502)
    return {
        'serialNo': serial_no,
        'expireSeconds': data.get('expireSeconds'),
        'resendIntervalSeconds': data.get('resendIntervalSeconds'),
    }


async def verify_code(mobile: str, code: str, user_ip: str = '') -> bool:
    """校验并消费验证码。仅 data.valid 严格为 True 才算通过。"""
    mobile = normalize_mobile(mobile)
    body = await sms_client.verify_code(mobile, code, user_ip)
    if body.get('code') not in (0, '0'):
        raise SmsLoginError(str(body.get('msg') or '校验失败'),
                            status=_http_status_for_code(body.get('code')))
    data = body.get('data') or {}
    return data.get('valid') is True


def _http_status_for_code(code) -> int:
    """把短信平台业务 code 映射为代理层 HTTP 状态码。

    注意：HTTP 非 200 属于通信异常（已在 sms_client 层抛 503/502），这里的 code
    是 CommonResult 里的业务 code。平台约定：400 参数/票据、401 应用/租户、
    403 签名/重放、429 频控。其余非零视为上游业务异常。
    """
    if code in (400, '400'):
        return 400
    if code in (401, '401'):
        return 401
    if code in (403, '403'):
        return 403
    if code in (429, '429'):
        return 429
    return 502
