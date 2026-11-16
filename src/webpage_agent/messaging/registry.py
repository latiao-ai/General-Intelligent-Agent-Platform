"""渠道清单（manifest）与工厂：加一个平台 = 加一份清单 + 一个 Transport。

界面完全是清单驱动的：后端返回字段元数据（env 键名、是否已配置、掩码后的
提示），前端渲染成表单。加渠道不用改前端，也不用在控制面里写
`if channel == ...`。

密钥只从环境变量（或个人微信的扫码登录态）读，**不落 `im_channels.config_json`**。
配置与密钥分离不是洁癖：配置可以导出、给同事看；密钥不行。

多实例的密钥也放在环境变量里，只是加一段后缀：实例 `feishu_bot_xzs` 读
`FEISHU_APP_ID__BOT_XZS`。**非默认实例不回落到基础键**——回落等于两个机器人
偷偷共用一个飞书应用，那正是这个功能要避免的事。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from webpage_agent.messaging.models import Capabilities

# 渠道 ID 固定：数据库、路由、前端深链都用它。改 ID 等于换一个渠道。
FEISHU = 'feishu'
WECOM = 'wecom'
WEIXIN = 'weixin'


def instance_env_suffix(instance_id: str) -> str:
    """实例 → 环境变量后缀。`bot_xzs` → `BOT_XZS`；空实例（存量那一份）→ 空串。

    只保留 ASCII 字母数字，其余一律换成下划线：环境变量名不允许中文和连字符，
    而机器人的 handle 是用户起的名字（`feishu-客服` 这种很常见）。
    首尾的下划线去掉——`feishu-客服` 应该变成 `FEISHU` 而不是 `FEISHU____`。
    """
    text = ''.join(ch if (ch.isascii() and ch.isalnum()) else '_'
                   for ch in str(instance_id or ''))
    return text.upper().strip('_')


@dataclass(frozen=True)
class EnvField:
    key: str                    # 传给 Transport 的 secrets 键
    env: str                    # 环境变量名（也是界面上的键名）
    label: str
    required: bool = True
    secret: bool = True
    help: str = ''
    url: str = ''


@dataclass(frozen=True)
class OptionField:
    key: str
    label: str
    kind: str = 'text'          # text | select | switch
    default: str = ''
    choices: tuple[tuple[str, str], ...] = ()
    help: str = ''


@dataclass(frozen=True)
class ChannelManifest:
    id: str
    name: str
    vendor: str
    tagline: str
    description: str
    docs_url: str
    priority: int
    capabilities: Capabilities
    env_fields: tuple[EnvField, ...]
    option_fields: tuple[OptionField, ...] = ()
    needs_public_url: bool = True
    default_dm_policy: str = 'pair'
    default_group_policy: str = 'mention'
    # 能不能一个部署挂多份（每个机器人一套应用）。
    # 飞书/企微可以：应用是各自独立的一对 AppID/Secret，回调靠验签区分归属。
    # 个人微信不行：它是一台手机上的一条 iLink 长轮询，登录态表也按 channel 存，
    # 多开只会互相踢下线。所以 weixin 只在默认实例下出现。
    multi_instance: bool = True
    setup_steps: tuple[str, ...] = ()
    transport: Callable[..., object] | None = field(default=None, compare=False)

    @property
    def callback_path(self) -> str:
        return f'/api/messaging/webhook/{self.id}'

    def env_name(self, item: EnvField, instance_id: str = '') -> str:
        """这个字段在该实例下真正去读的环境变量名。"""
        suffix = instance_env_suffix(instance_id)
        return f'{item.env}__{suffix}' if suffix else item.env

    def env_values(self, instance_id: str = '') -> dict[str, str]:
        return {item.key: os.getenv(self.env_name(item, instance_id), '').strip()
                for item in self.env_fields}

    def missing(self, instance_id: str = '') -> list[str]:
        return [item.label for item in self.env_fields
                if item.required
                and not os.getenv(self.env_name(item, instance_id), '').strip()]


def _load_feishu():
    from webpage_agent.messaging.transports.feishu import FeishuTransport
    return FeishuTransport


def _load_wecom():
    from webpage_agent.messaging.transports.wecom import WeComTransport
    return WeComTransport


def _load_weixin():
    from webpage_agent.messaging.transports.weixin import WeixinTransport
    return WeixinTransport


# 能力声明必须与 Transport.capabilities() 一致（单测会逐项比对）：
# 不支持卡片就别声明，Router 会按这里降级成纯文本。
FEISHU_CAPS = Capabilities(card=False, stream_edit=False, typing=False,
                           thread=True, reaction=True, group=True,
                           max_text_chars=10_000)
WECOM_CAPS = Capabilities(card=False, stream_edit=False, typing=False,
                          thread=False, reaction=False, group=True,
                          max_text_chars=2000)
WEIXIN_CAPS = Capabilities(card=False, stream_edit=False, typing=True,
                           thread=False, reaction=False, group=False,
                           max_text_chars=2000)

CHANNELS: tuple[ChannelManifest, ...] = (
    ChannelManifest(
        id=FEISHU,
        name='飞书',
        vendor='字节跳动 · 开放平台',
        tagline='企业自建应用 · 事件订阅回调',
        description=('在飞书开放平台创建企业自建应用，开启 im:message 相关权限，'
                     '把事件订阅地址指向本服务的回调地址。支持单聊与群聊 @。'),
        docs_url='https://open.feishu.cn/document/home/index',
        priority=1,
        capabilities=FEISHU_CAPS,
        env_fields=(
            EnvField('app_id', 'FEISHU_APP_ID', 'App ID',
                     help='开放平台 → 凭证与基础信息'),
            EnvField('app_secret', 'FEISHU_APP_SECRET', 'App Secret',
                     help='开放平台 → 凭证与基础信息'),
            EnvField('verification_token', 'FEISHU_VERIFICATION_TOKEN',
                     'Verification Token', required=False, secret=True,
                     help='事件订阅未开启加密时用于校验来源'),
            EnvField('encrypt_key', 'FEISHU_ENCRYPT_KEY', 'Encrypt Key',
                     required=False, secret=True,
                     help='事件订阅加密密钥；强烈建议开启，开启后必须填'),
            EnvField('base_url', 'FEISHU_BASE_URL', 'API 域名',
                     required=False, secret=False,
                     help='默认 https://open.feishu.cn；Lark 国际版填对应的域名'),
        ),
        option_fields=(
            OptionField('home_chat_id', '默认会话 ID',
                        help='测试发送与主动通知的目标 chat_id'),
            OptionField('display_name', '显示名称', help='仅用于本界面'),
        ),
        needs_public_url=True,
        setup_steps=(
            '在开放平台创建「企业自建应用」。',
            '权限管理里勾选 im:message、im:message:send_as_bot（以及群信息读取）。',
            '事件订阅选择「请求地址」，填本页给出的回调地址。',
            '把 App ID / App Secret / Encrypt Key 写进 .env 后重启 Worker。',
            '发布应用，并在飞书里给应用发一条消息验证。',
        ),
        transport=_load_feishu,
    ),
    ChannelManifest(
        id=WECOM,
        name='企业微信',
        vendor='腾讯 · 企业微信',
        tagline='自建应用 · 回调 + 主动发送',
        description=('企业微信管理后台创建自建应用，接收消息模式选「API 接收」，'
                     '配置 URL / Token / EncodingAESKey。回调必须公网 HTTPS。'),
        docs_url='https://developer.work.weixin.qq.com/document/path/90930',
        priority=2,
        capabilities=WECOM_CAPS,
        env_fields=(
            EnvField('corp_id', 'WECOM_CORP_ID', '企业 ID (CorpID)'),
            EnvField('agent_id', 'WECOM_AGENT_ID', '应用 AgentId',
                     secret=False, help='管理后台 → 应用管理 → 自建应用'),
            EnvField('secret', 'WECOM_SECRET', '应用 Secret',
                     help='用于获取 access_token'),
            EnvField('token', 'WECOM_TOKEN', '回调 Token',
                     help='接收消息 → API 接收 里配置的 Token'),
            EnvField('aes_key', 'WECOM_AES_KEY', 'EncodingAESKey',
                     help='接收消息 → API 接收 里配置的 EncodingAESKey'),
            EnvField('base_url', 'WECOM_BASE_URL', 'API 域名',
                     required=False, secret=False,
                     help='默认 https://qyapi.weixin.qq.com'),
            EnvField('bot_webhook', 'WECOM_BOT_WEBHOOK', '群机器人 Webhook',
                     required=False,
                     help='可选：只用来发通知（群机器人不能收消息）'),
        ),
        option_fields=(
            OptionField('home_chat_id', '默认成员/会话 ID',
                        help='测试发送用；填成员 UserID 或群聊 chatid'),
            OptionField('display_name', '显示名称'),
        ),
        needs_public_url=True,
        default_group_policy='disabled',
        setup_steps=(
            '企业微信管理后台 → 应用管理 → 创建自建应用。',
            '「接收消息」→ 设置 API 接收：URL 填本页回调地址，生成 Token 与 EncodingAESKey。',
            '把 CorpID / AgentId / Secret / Token / EncodingAESKey 写进 .env。',
            '在应用的「可见范围」里加入要对话的成员。',
            '配置可信 IP（如平台要求），重启 Worker 后发消息验证。',
        ),
        transport=_load_wecom,
    ),
    ChannelManifest(
        id=WEIXIN,
        name='个人微信',
        vendor='腾讯 · iLink Bot',
        tagline='扫码登录 · 尽力而为的私聊通道',
        description=('通过腾讯 iLink Bot API 接入：扫码后以 bot 身份收发私聊。'
                     '不是逆向协议，也不使用 hook；能力有限：群聊基本收不到，'
                     '会话会过期，需要重新扫码。默认关闭。'),
        docs_url='',
        priority=3,
        capabilities=WEIXIN_CAPS,
        env_fields=(
            EnvField('base_url', 'WEIXIN_BASE_URL', 'iLink 域名',
                     required=False, secret=False,
                     help='默认 https://ilinkai.weixin.qq.com'),
            EnvField('cdn_base_url', 'WEIXIN_CDN_BASE_URL', '媒体 CDN',
                     required=False, secret=False,
                     help='默认 https://novac2c.cdn.weixin.qq.com/c2c'),
        ),
        option_fields=(
            OptionField('home_chat_id', '默认会话 ID',
                        help='测试发送的目标；iLink 的 to_user_id'),
            OptionField('display_name', '显示名称'),
        ),
        needs_public_url=False,
        default_group_policy='disabled',
        multi_instance=False,
        setup_steps=(
            '本页点「扫码登录」，用微信扫描二维码并确认。',
            '登录成功后 Worker 会自动开始长轮询，不需要公网地址。',
            '让对方先给这个 bot 发一条消息，回复才能发出去（iLink 的会话限制）。',
            '会话过期（errcode=-14）时重新扫码；网关不会自动重试风暴。',
        ),
        transport=_load_weixin,
    ),
)

BY_ID = {item.id: item for item in CHANNELS}


def get_manifest(channel_id: str) -> ChannelManifest | None:
    return BY_ID.get(str(channel_id or ''))


def channels_for(instance_id: str = '') -> tuple[ChannelManifest, ...]:
    """这个实例下有哪些渠道。

    默认实例（`''`，存量那一份）包含全部三个——包括只能挂一份的个人微信；
    机器人实例只含支持多开的（飞书、企微）。否则界面上会出现一个永远配不起来的
    「个人微信」tab，扫码还会把默认实例那条登录态踢掉。
    """
    if not str(instance_id or ''):
        return CHANNELS
    return tuple(item for item in CHANNELS if item.multi_instance)


def secrets_for(manifest: ChannelManifest, extra: dict | None = None,
                instance_id: str = '') -> dict[str, str]:
    """环境变量 + 运行时登录态合并成 Transport 的 secrets。

    `instance_id` 为空 = 存量那一份，读基础键；非空读带后缀的键，**不回落**。
    """
    values = manifest.env_values(instance_id)
    for key, value in (extra or {}).items():
        if value not in (None, ''):
            values[str(key)] = str(value)
    return values


def diagnose(manifest: ChannelManifest, *, login_state: dict | None,
             public_base_url: str,
             instance_id: str = '') -> tuple[bool, list[str], str]:
    """返回 `(是否可启动, 缺什么, 回调地址)`。纯函数，单测覆盖。"""
    missing = manifest.missing(instance_id)
    callback = ''
    if manifest.needs_public_url:
        base = str(public_base_url or '').rstrip('/')
        if not base:
            missing.append('公网 HTTPS 地址（IM_PUBLIC_BASE_URL）')
        elif not base.startswith('https://'):
            missing.append('公网地址必须是 https://')
        else:
            callback = f'{base}{manifest.callback_path}'
    if manifest.id == WEIXIN:
        state = login_state or {}
        if not str(state.get('token') or '').strip():
            missing.append('扫码登录（个人微信）')
    return (not missing), missing, callback
