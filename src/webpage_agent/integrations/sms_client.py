"""内部短信验证码平台的开放接口客户端（AK/SK 签名 + 代理转发）。

对接文档：`内部短信验证码平台对接文档.md`，基础路径 `{服务根地址}/open-api/sms`。

职责边界（与 `integrations/` 其它模块一致）：只负责"连出去、拿到结构化结果"，
不做任何业务判断。这里的调用方是后端代理层，浏览器**永远不能**拿到 appSecret。

签名规则（文档 §签名）：

    待签名字符串 = appKey + timestamp + nonce   # 三段直接拼接，无分隔符
    签名 = HMAC-SHA256(原始 appSecret, 待签名字符串 UTF-8 字节) 的 64 位小写 hex

每次请求（含重试）都必须换新的 timestamp / nonce / sign。`X-User-IP` 由后端按
可信代理配置解析真实用户地址后传入，不能照抄浏览器提交的同名请求头。
"""
import hashlib
import hmac
import secrets
import time
from typing import Any

import httpx

from webpage_agent.core.config import settings

# 基础路径。根地址从服务端环境读取，禁止前端指定任意目标 URL。
_BASE_PATH = '/open-api/sms'

# 场景：文档规定当前固定为 101，登录/注册/重置密码共用此通用场景。
SCENE_LOGIN = 101


class SmsPlatformError(RuntimeError):
    """短信平台通信异常或业务失败。

    `status` 供代理层决定 HTTP 状态码；`code` / `msg` 保留平台原始值供排查。
    """

    def __init__(self, message: str, *, status: int = 502, code: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def sign(app_key: str, app_secret: str, timestamp: str, nonce: str) -> str:
    """按文档算法计算签名。三段直接拼接，输出 64 位小写 hex。"""
    raw = app_key + timestamp + nonce
    return hmac.new(app_secret.encode('utf-8'),
                    raw.encode('utf-8'), hashlib.sha256).hexdigest()


def _build_headers(user_ip: str = '') -> dict[str, str]:
    """构造每次请求的公共鉴权头。每次调用生成全新的 timestamp / nonce / sign。"""
    app_key = settings.sms_app_key
    app_secret = settings.sms_app_secret
    if not app_key or not app_secret:
        raise SmsPlatformError('短信平台凭据未配置（SMS_APP_KEY / SMS_APP_SECRET）',
                               status=503)
    timestamp = str(int(time.time() * 1000))
    nonce = secrets.token_hex(16)  # 32 字符随机串，等价 UUID 强度
    headers = {
        'Content-Type': 'application/json',
        'X-Sms-App-Key': app_key,
        'X-Sms-Timestamp': timestamp,
        'X-Sms-Nonce': nonce,
        'X-Sms-Sign': sign(app_key, app_secret, timestamp, nonce),
    }
    if user_ip:
        headers['X-User-IP'] = user_ip
    return headers


def _base_url() -> str:
    base = settings.sms_base_url
    if not base:
        raise SmsPlatformError('短信平台服务根地址未配置（SMS_BASE_URL）', status=503)
    return base.rstrip('/')


async def _post(path: str, payload: dict[str, Any], user_ip: str = '') -> dict[str, Any]:
    """向短信平台发 POST，原样返回 CommonResult（{code,msg,data}），不做业务解包。"""
    url = _base_url() + _BASE_PATH + path
    try:
        async with httpx.AsyncClient(timeout=settings.sms_timeout_seconds,
                                     follow_redirects=False) as client:
            reply = await client.post(url, json=payload, headers=_build_headers(user_ip))
    except httpx.HTTPError as exc:
        # 连接超时 / 中断属于通信异常，不能当成"验证码不匹配"向上抛业务语义。
        raise SmsPlatformError('短信平台不可达', status=503) from exc

    if reply.status_code >= 500:
        raise SmsPlatformError('短信平台返回异常', status=502,
                               code=reply.status_code)
    try:
        body = reply.json()
    except ValueError:
        raise SmsPlatformError('短信平台响应无法解析', status=502) from None
    if not isinstance(body, dict):
        raise SmsPlatformError('短信平台响应形状异常', status=502)
    return body


async def _get(path: str) -> dict[str, Any]:
    """向短信平台发 GET（仅 send-log 用）。"""
    url = _base_url() + _BASE_PATH + path
    try:
        async with httpx.AsyncClient(timeout=settings.sms_timeout_seconds,
                                     follow_redirects=False) as client:
            reply = await client.get(url, headers=_build_headers())
    except httpx.HTTPError as exc:
        raise SmsPlatformError('短信平台不可达', status=503) from exc
    if reply.status_code >= 500:
        raise SmsPlatformError('短信平台返回异常', status=502,
                               code=reply.status_code)
    try:
        body = reply.json()
    except ValueError:
        raise SmsPlatformError('短信平台响应无法解析', status=502) from None
    if not isinstance(body, dict):
        raise SmsPlatformError('短信平台响应形状异常', status=502)
    return body


async def get_captcha(user_ip: str) -> dict[str, Any]:
    """获取滑块。返回完整 CommonResult；调用方须同时检查 code==0 且 data.repCode=='0000'。"""
    return await _post('/captcha/get', {'captchaType': 'blockPuzzle'}, user_ip)


async def check_captcha(token: str, point_json: str, mobile: str,
                        user_ip: str) -> dict[str, Any]:
    """校验滑块并发放服务端票据。返回完整 CommonResult。"""
    return await _post('/captcha/check', {
        'captchaType': 'blockPuzzle',
        'token': token,
        'pointJson': point_json,
        'mobile': mobile,
    }, user_ip)


async def send_code(mobile: str, captcha_verification: str, user_ip: str,
                    ext_info: dict[str, Any] | None = None) -> dict[str, Any]:
    """带票据发送短信验证码。返回完整 CommonResult（成功含 serialNo）。"""
    payload: dict[str, Any] = {
        'mobile': mobile,
        'scene': SCENE_LOGIN,
        'captchaVerification': captcha_verification,
    }
    if ext_info:
        payload['extInfo'] = ext_info
    return await _post('/send-code', payload, user_ip)


async def verify_code(mobile: str, code: str, user_ip: str = '') -> dict[str, Any]:
    """校验并消费短信验证码。返回完整 CommonResult；调用方须检查 data.valid 严格等于 True。"""
    payload = {'mobile': mobile, 'code': code, 'scene': SCENE_LOGIN}
    return await _post('/verify-code', payload, user_ip)


async def send_log(serial_no: str) -> dict[str, Any]:
    """按 serialNo 查询发送记录。serialNo 必须保持字符串。"""
    return await _get(f'/send-log?serialNo={serial_no}')
