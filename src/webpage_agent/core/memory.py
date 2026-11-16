"""会话记忆：把更早的对话压成摘要 + 关键事实。

长会话不可能每轮都把全部原文塞进 prompt。这里做两件事：

1. **滚动摘要**：未摘要的消息超过阈值时，把最老的一段合并进已有摘要；
2. **关键事实**：结构化 kv（目标 / 已否决 / 关键约束…），覆盖式更新，
   注入时优先级高于摘要——它是"硬结论"，摘要只是叙述。

沿用画像那套原则（文件即记忆的延伸）：

- 产物是**人可读的文本**，用户可以在界面里直接改（见 `/api/sessions/{id}/memory`）；
- **原文永远留在 messages 表**，摘要只是"注入视图"，随时可以重新生成；
- 压缩失败不影响任务：继续用旧摘要，绝不因为压缩把一轮对话搞挂。
"""
import json
import logging
import re
from dataclasses import dataclass, field

from webpage_agent.agent import context
from webpage_agent.core import profile
from webpage_agent.core.config import settings
from webpage_agent.core.schemas import SessionMemory
from webpage_agent.core.services import (
    AttemptTimeout, StructuredOutputError, structured_call)

logger = logging.getLogger(__name__)

# 最近这么多条永远保留原文，不进摘要
KEEP_RECENT = 6
# 未摘要条数超过这个就压缩
MAX_MESSAGES = 12
# 或者未摘要部分超过这个 token 数
MAX_TOKENS = 3000

SUMMARY_SYSTEM = (
    '你负责压缩对话历史。把「已有摘要」与「需要合并的对话」合成一份新的记忆，'
    '输出两个字段：'
    'summary：不超过 300 字的中文摘要，必须保留用户的目标、已经完成的关键动作、'
    '已经确认的结论、已被否决的方案、以及尚未解决的问题；丢弃寒暄与重复内容。'
    'facts：从整段对话里提炼的、后续会用到的硬结论，键用简短中文（如"目标""已否决""约束"），'
    '值一句话；没有就留空 {}}。'
    '只写对话里明确出现过的信息，不要推测、不要编造。'
)


def parse_facts(text: str) -> dict:
    """把「键：值」多行文本解析成字典（界面编辑用）。"""
    facts = {}
    for line in (text or '').splitlines():
        line = line.strip().lstrip('-').strip()
        if not line:
            continue
        for sep in ('：', ':'):
            if sep in line:
                key, value = line.split(sep, 1)
                if key.strip():
                    facts[key.strip()] = value.strip()
                break
    return facts


def render_facts(facts: dict) -> str:
    return '\n'.join(f'{key}：{value}' for key, value in (facts or {}).items())


def needs_compaction(messages: list[dict]) -> bool:
    if len(messages) <= MAX_MESSAGES:
        chars = sum(len(m.get('content') or '') for m in messages)
        return context.estimate_tokens('x' * chars) > MAX_TOKENS
    return True


async def compact(store, session_id: str) -> dict:
    """必要时压缩会话记忆，返回最新记忆（含 compacted/folded 供调用方播报）。

    任何异常都退化成"不压缩"，绝不向上抛：压缩是优化，不是任务的前置条件。
    """
    memory = await store.memory(session_id)
    pending = await store.messages_after(session_id, memory.get('upto') or 0)
    if not needs_compaction(pending):
        return {**memory, 'compacted': False}

    fold = pending[:-KEEP_RECENT]
    if not fold:
        return {**memory, 'compacted': False}

    payload = json.dumps({
        '已有摘要': memory.get('summary') or '',
        '已有事实': memory.get('facts') or {},
        '需要合并的对话': [{'role': m['role'], 'content': m['content']}
                          for m in fold],
    }, ensure_ascii=False)
    try:
        result, _ = await structured_call(
            SessionMemory, SUMMARY_SYSTEM, payload,
            model=None, disable_thinking=True)
        data = result.model_dump()
    except Exception:
        return {**memory, 'compacted': False}

    summary = (data.get('summary') or '').strip()
    facts = data.get('facts') or {}
    upto = fold[-1]['id']
    await store.set_memory(session_id, summary=summary, facts=facts, upto=upto)
    return {'summary': summary, 'facts': facts, 'upto': upto,
            'compacted': True, 'folded': len(fold)}


def render_long_term(user_rows: list[dict], workspace_rows: list[dict],
                     bot_rows: list[dict] | None = None,
                     max_chars: int = 4000,
                     override_keys=None) -> str:
    """把用户级/工作区/机器人长期记忆拼成给模型的 <memory> 块。

    `bot_rows` 是**这个机器人自己**的记忆（scope_type='bot'）：机器人之间有
    独立的记忆，才谈得上"各自的专业积累"，否则所有机器人共享一份用户画像，
    角色分工只剩提示词里的一句话。

    `override_keys` 是**用户画像里已经有的键**：这些事实会由画像段单独注入，
    这里不再重复。两边取值不一致时以画像为准——画像是用户能直接改、带版本号
    的那一份，而 user scope 记忆多半是自动提取来的；同一条偏好出现两个值，
    模型只会挑一个信，结果就变成"说过的话过两轮就忘了"。
    """
    skip = {str(key) for key in (override_keys or ()) if str(key).strip()}

    def lines(rows, skip_keys=()):
        out = []
        for row in rows or []:
            predicate = str(row.get('predicate') or '').strip()
            value = str(row.get('value') or '').strip()
            if predicate and value and predicate not in skip_keys:
                out.append(f'- {predicate}：{value}')
        return out
    user_lines = lines(user_rows, skip)
    ws_lines = lines(workspace_rows)
    bot_lines = lines(bot_rows)
    if not user_lines and not ws_lines and not bot_lines:
        return ''
    parts = ['<memory>', '以下是跨会话记忆，只作为背景；当前用户输入和当前事实优先。']
    if user_lines:
        parts.append('用户长期记忆：')
        parts.extend(user_lines)
    if ws_lines:
        parts.append('当前工作区记忆：')
        parts.extend(ws_lines)
    if bot_lines:
        parts.append('这个机器人自己的长期记忆：')
        parts.extend(bot_lines)
    parts.append('</memory>')
    text = '\n'.join(parts)
    return text[:max_chars]


_MEMORY_CUE = re.compile(
    r'(记住|记一下|以后都|以后请|始终|总是|不要|偏好|习惯|'
    r'我的名字|我叫|叫我|我住在|我的城市|我喜欢|我不喜欢|'
    r'我来自|我是[^，。,\s]{0,8}人|老家|籍贯|家乡|现居|'
    r'remember|always|never)',
    re.IGNORECASE,
)

# 画像允许自动新增的键：「我是谁」这一类稳定信息。
#
# 记忆那本账可以很大，画像必须短——它每轮都整段塞进 system prompt。
# 一旦被自动提取撑成第二份记忆，prompt 成本和噪音都会失控，所以这里用
# 白名单而不是黑名单：宁可漏记几条，也不能让"目标""上次报错"混进档案。
_PROFILE_KEY_RE = re.compile(
    r'(称呼|姓名|名字|昵称|城市|所在地|所在城市|常住|居住|现居|'
    r'籍贯|老家|家乡|出生地|来自|'
    r'职业|行业|岗位|职位|身份|公司|单位|团队|语言|语种|时区|风格|语气|格式|偏好)')

# 画像条目上限：新增只在上限内受理。
PROFILE_MAX_KEYS = 12


@dataclass(frozen=True)
class ExtractResult:
    """一次抽取的产出与成本。

    `saved` 是写进记忆库的条数；`learned` 是**画像真正发生变化**的条目——
    只有它才配得上界面上那句「已记住」，模型自己复述一遍不算。
    `usage` 直接来自 provider，用来回答"记一条要花多少 token"。

    `error` 是**失败原因**，只在"该抽却没抽成"时非空（超时/模型不可用/
    结构不合法）。抽到空结果是正常情况——这轮本来就没说什么值得记的，
    不能报成失败，否则每句寒暄都弹一条警告。调用方拿它写回执：以前这里
    全是 except 静默，模型嘴里说着"已记录"、画像却一动不动，用户只能
    打开面板自己发现对不上账。
    """
    saved: int = 0
    learned: dict = field(default_factory=dict)
    usage: dict | None = None
    error: str = ''


def _usage_brief(usage: dict | None) -> str:
    """把 usage 压成一行，方便在日志里 grep 成本。"""
    if not isinstance(usage, dict):
        return 'n/a'
    parts = []
    for key, label in (('input_tokens', 'in'), ('output_tokens', 'out'),
                       ('total_tokens', 'total')):
        if isinstance(usage.get(key), int):
            parts.append(f'{label}={usage[key]}')
    details = usage.get('input_token_details')
    if isinstance(details, dict) and isinstance(details.get('cache_read'), int):
        parts.append(f'cache={details["cache_read"]}')
    return ' '.join(parts) or 'n/a'


def is_profile_key(key: str) -> bool:
    """这个记忆键该不该同步进画像（身份型 vs 项目型）。"""
    return bool(_PROFILE_KEY_RE.search(str(key or '')))


def _failure_reason(exc: BaseException) -> str:
    """把抽取异常翻成一句用户能看懂的话。

    只回答"这轮为什么没记住"这一件事，不把 provider 的原始报错甩到界面上：
    用户要的是账对不对得上，不是堆栈。三种情形对用户的意义不同，所以分开说：
    超时和结构不合法下一轮多半能自愈，模型不可用则要人去查配置。
    """
    if isinstance(exc, (AttemptTimeout, TimeoutError)):
        return '记忆抽取超时'
    if isinstance(exc, StructuredOutputError):
        return '记忆抽取返回的结构不合法'
    return '记忆抽取调用失败'

_MEMORY_EXTRACT_SYSTEM = (
    '你负责从一轮对话中提取**用户长期记忆**。只提取用户明确表达、以后仍然有用的：'
    '身份、长期偏好、固定规则、长期项目事实。不要提取一次性请求、临时天气、'
    '工具报错、搜索结果、密码/Token/API Key 等秘密。'
    '自我介绍里的姓名、称呼、城市/籍贯、职业、语言、习惯表达要记，'
    '寒暄和当轮的临时问题不要记。'
    '值写事实本身（如"浙江台州"），不要写成"用户来自浙江台州"这类带主语的句子。'
    '输出 facts：键简短中文，值一句话；最多 6 条；没有就留空。'
)


def has_memory_cue(text: str) -> bool:
    return bool(_MEMORY_CUE.search(str(text or '')))


def should_extract(text: str) -> bool:
    """这一轮要不要跑抽取。

    `always`（默认）把"值不值得记"整个交给模型：先抽出来，再由
    `is_profile_key` + `PROFILE_MAX_KEYS` 卡住写画像的部分。
    关键词表永远漏——"我来自浙江台州"就不在表里，那一轮 cue 不命中，
    画像停在上一轮，而模型的回答里已经写着"已记录"了，用户对不上账。

    `cue` 是省成本的旧行为，`off` 全关。
    """
    mode = str(getattr(settings, 'memory_extract_mode', 'always') or '').lower()
    if mode == 'off':
        return False
    text = str(text or '').strip()
    if mode == 'cue':
        return has_memory_cue(text)
    # 「好的」「谢谢」这类一句话寒暄也过一次模型不值：一次抽取实测约 600 token。
    # 这不是关键词表，只是成本下限——真正的判断仍然在模型那边。
    return len(text) >= 4


async def extract_user_memory(store, *, scope_id: str, tenant_id: str,
                              user_message: str, answer: str,
                              created_by: str = 'default',
                              bot_id: str = '') -> ExtractResult:
    """从一轮对话提取长期记忆并写入 user scope。

    失败不抛异常（抽不到记忆不该让一轮聊天失败），但也不静默：原因放进
    `error` 交给调用方播报。以前这里 `except: return ExtractResult()`，
    界面上什么都看不到，用户只能靠打开画像面板才发现"它说记了，其实没记"。

    `bot_id` 非空时同一批事实**同时**写一份到机器人自己的 scope：用户对
    "这个机器人"的长期要求（"以后报告都用中文"）不该被另一个角色的机器人
    继承（"以后代码都加注释"）。

    画像里**已经有**的键会同步改一次画像：注入时画像是优先的那一份，不同步的话
    "称呼：浩南"留在画像里，用户改口"以后叫我南哥"只进了记忆，下一轮照样叫浩南——
    改了等于没改。

    画像里**没有**的键分两种处理：身份型的（称呼/城市/职业/语言…）可以新增，
    项目型的不给进——那份档案的定位是"我是谁"，不是记忆的第二份副本。
    只改已有键的那版有个死结：从没填过的画像永远长不出第一条，面板一直显示
    「尚未建立」，聊一百轮还是空的（自动新增只在任务规划里发生）。
    """
    import json
    from webpage_agent.core.schemas import SessionMemory
    payload = json.dumps({
        '用户': user_message,
        '助手': answer,
    }, ensure_ascii=False)
    try:
        result, usage = await structured_call(
            SessionMemory, _MEMORY_EXTRACT_SYSTEM, payload,
            model=None, disable_thinking=True)
        facts = result.model_dump().get('facts') or {}
    except Exception as exc:                       # noqa: BLE001
        # 失败要留痕也要能播报：warning 进日志给排障，error 交给调用方写回执。
        # CancelledError 不在此列（BaseException），用户取消照常向上传播。
        reason = _failure_reason(exc)
        logger.warning('记忆抽取失败 scope=%s reason=%s', scope_id, reason,
                       exc_info=True)
        return ExtractResult(error=reason)
    items = []
    for key, value in list(facts.items())[:6]:
        key = str(key).strip()
        value = str(value).strip()
        if key and value and len(value) <= 500:
            items.append((key, value))
    if not items:
        return ExtractResult(usage=usage)

    try:
        profiled = profile.load(user=scope_id)
    except Exception:                              # noqa: BLE001
        profiled = {}
    overrides = {key: value for key, value in items
                 if key in profiled and profiled[key] != value}
    # 空画像的自举：只放行身份型的**新**键，并卡住总量。
    # 这条以前是缺的——画像是空的，就永远没有键可改，自动链路彻底停摆。
    if len(profiled) < PROFILE_MAX_KEYS:
        for key, value in items:
            if key in profiled or key in overrides or not is_profile_key(key):
                continue
            overrides[key] = value
            if len(profiled) + len(overrides) >= PROFILE_MAX_KEYS:
                break
    learned = {}
    if overrides:
        try:
            learned = profile.merge(overrides, user=scope_id)
        except Exception:                          # noqa: BLE001
            logger.debug('同步画像失败 scope=%s', scope_id, exc_info=True)

    count = 0
    for key, value in items:
        saved = await store.remember(
            scope_type='user', scope_id=scope_id, subject='用户',
            predicate=key, value=value, source='auto',
            created_by=created_by, tenant_id=tenant_id)
        if saved:
            count += 1
        if bot_id:
            try:
                await store.remember(
                    scope_type='bot', scope_id=bot_id, subject='用户',
                    predicate=key, value=value, source='auto',
                    created_by=created_by, tenant_id=tenant_id)
            except Exception:                      # noqa: BLE001
                pass
    # 成本可见：每轮闲聊都会跑一次抽取，一条 in/out 就是它的真实开销。
    logger.info('记忆抽取 scope=%s saved=%d learned=%s usage=%s',
                scope_id, count, learned or '无', _usage_brief(usage))
    return ExtractResult(saved=count, learned=learned, usage=usage)


def render_session_memory(summary: str, facts: dict, max_chars: int = 2400) -> str:
    """把滚动摘要和会话事实拼成聊天提示里的短期记忆块。"""
    summary = str(summary or '').strip()
    facts = {str(k): str(v) for k, v in (facts or {}).items() if str(k).strip()}
    if not summary and not facts:
        return ''
    parts = ['<session_memory>', '这是本会话更早对话的摘要；最近几轮原文仍然保留。']
    if summary:
        parts.append('摘要：' + summary)
    if facts:
        parts.append('关键事实：')
        parts.extend(f'- {key}：{value}' for key, value in facts.items())
    parts.append('</session_memory>')
    return '\n'.join(parts)[:max_chars]
