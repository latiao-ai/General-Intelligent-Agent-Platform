"""渠道状态：**派生**，不存储。

Hermes 的做法值得照抄：配置面只读三个来源（配置开关、凭据是否齐全、运行态
快照），用一个纯函数推最终状态。这样两个界面（左侧列表、详情页）永远同色——
颜色映射只写一次。

运行态由 Worker 写入 `im_runtime`（`connected / connecting / retrying / fatal /
paused / startup_failed`）；控制态由本模块派生：

    disabled          未启用
    not_configured    启用了但缺凭据/公网地址/扫码登录
    gateway_stopped   网关进程没在跑（没有 messaging 心跳）
    pending_restart   网关在跑，但这个渠道还没有运行态快照，或配置指纹变了
    startup_failed    上次启动失败，且不是可自愈的错误
"""
from __future__ import annotations

import hashlib
import json
import time

# 运行态里表示"已经在跑"的值；其余值仍按运行态原样返回，便于排障。
RUNNING_STATES = ('connected', 'connecting', 'retrying', 'paused')
ALL_STATES = RUNNING_STATES + ('fatal', 'startup_failed', 'disabled',
                               'not_configured', 'gateway_stopped',
                               'pending_restart')

# 同一个状态在两处界面必须同色。新增状态时**只改这里**。
TONE = {
    'connected': 'good',
    'disabled': 'muted',
    'fatal': 'bad',
    'startup_failed': 'bad',
}

LABELS = {
    'connected': '已连接',
    'connecting': '连接中',
    'retrying': '重连中',
    'paused': '已暂停',
    'fatal': '启动失败',
    'startup_failed': '启动失败',
    'disabled': '未启用',
    'not_configured': '待配置',
    'gateway_stopped': '网关未运行',
    'pending_restart': '待生效',
}


def tone(state: str) -> str:
    """状态 → 颜色档（good / warn / bad / muted）。未知状态按 warn。"""
    return TONE.get(state, 'warn')


def label(state: str) -> str:
    return LABELS.get(state, state or '未知')


def config_fingerprint(*, enabled: bool, bot_id: str, dm_policy: str,
                       group_policy: str, allowed_users, config: dict,
                       secrets: dict, public_base_url: str) -> str:
    """配置指纹：只进哈希，不出明文。密钥轮换也能被感知（不暴露旧值）。"""
    material = {
        'enabled': bool(enabled),
        'bot_id': str(bot_id or ''),
        'dm_policy': str(dm_policy or ''),
        'group_policy': str(group_policy or ''),
        'allowed_users': sorted(str(item) for item in (allowed_users or [])),
        'config': config or {},
        'secrets': {key: str(value or '') for key, value in (secrets or {}).items()},
        'public_base_url': str(public_base_url or ''),
    }
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def derive(*, enabled: bool, configured: bool, gateway_alive: bool,
           runtime: dict | None, fingerprint: str,
           now_ts: float | None = None,
           stale_seconds: float = 90.0) -> dict:
    """纯函数：配置 + 运行态 → 展示状态。

    `runtime` 为 `im_runtime` 行（没有就是 None）。`fingerprint` 是当前配置的
    指纹；与运行态里记录的不一致，说明"配置改了但还没重建适配器"。
    """
    stamp = time.time() if now_ts is None else float(now_ts)
    if not enabled:
        return {'state': 'disabled', 'reason': '未启用', 'tone': tone('disabled')}
    if not configured:
        return {'state': 'not_configured', 'reason': '凭据或接入信息不完整',
                'tone': tone('not_configured')}
    if not gateway_alive:
        return {'state': 'gateway_stopped',
                'reason': '消息网关未运行（需要 Worker 进程）',
                'tone': tone('gateway_stopped')}
    if not runtime or not runtime.get('state'):
        return {'state': 'pending_restart', 'reason': '等待网关加载',
                'tone': tone('pending_restart')}
    try:
        updated = float(runtime.get('updated_at_ts') or 0)
    except (TypeError, ValueError):
        updated = 0.0
    if updated and (stamp - updated) > stale_seconds:
        return {'state': 'pending_restart',
                'reason': '运行态已过期（网关可能刚重启）',
                'tone': tone('pending_restart')}
    if runtime.get('fingerprint') and runtime['fingerprint'] != fingerprint:
        return {'state': 'pending_restart',
                'reason': '配置已更新，等待网关重建连接',
                'tone': tone('pending_restart')}
    state = str(runtime.get('state') or '')
    if state == 'startup_failed':
        return {'state': 'startup_failed',
                'reason': str(runtime.get('detail') or '启动失败'),
                'tone': tone('startup_failed')}
    return {'state': state or 'pending_restart',
            'reason': str(runtime.get('detail') or ''),
            'tone': tone(state or 'pending_restart'),
            'error_code': str(runtime.get('error_code') or '')}
