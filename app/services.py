import re
import json
import time
import ipaddress
from urllib.parse import urlsplit, urlunsplit
from pydantic import ValidationError
from langchain_core.utils.json import parse_partial_json
from langchain_openai import ChatOpenAI
from tavily import AsyncTavilyClient
from app.config import settings


def clean_public_url(value: str) -> str:
    p = urlsplit(value.strip())
    if p.scheme.lower() not in {'http', 'https'} or not p.hostname:
        raise ValueError('只支持 HTTP/HTTPS URL')
    if p.username or p.password:
        raise ValueError('URL 不允许包含账户凭据')
    host = p.hostname.lower().rstrip('.')
    if '.' not in host and ':' not in host:
        raise ValueError('拒绝单标签主机')
    if host == 'localhost' or host.endswith(('.local', '.localhost', '.internal')):
        raise ValueError('拒绝本地地址')
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None and not addr.is_global:
        raise ValueError('拒绝非公共 IP')
    if p.port not in (None, 80, 443):
        raise ValueError('首版只支持标准网页端口')
    # 保留查询参数，移除 fragment；域名基本规范化。
    netloc = '[' + host + ']' if ':' in host else host
    if p.port:
        netloc += ':' + str(p.port)
    return urlunsplit((p.scheme.lower(), netloc, p.path or '/', p.query, ''))


def urls_from_input(text: str) -> list[str]:
    candidates = re.findall(r'https?://[^\s<>"\u3000]+', text)
    result = []
    for value in candidates:
        value = value.rstrip('.,;!?)]}，。；！？）】、')
        value = clean_public_url(value)
        if value not in result:
            result.append(value)
    return result[:settings.max_pages]


def new_llm(model: str | None = None, disable_thinking: bool = False):
    options = dict(
        model=model or settings.llm_model,
        api_key=settings.llm_key,
        temperature=0,
        timeout=300,
        max_retries=1,
    )
    if settings.llm_base_url:
        options['base_url'] = settings.llm_base_url
    if disable_thinking:
        # 只有 'none' 与 thinking.disabled 能真正关掉推理；
        # reasoning_effort 的 low / minimal 会被网关忽略（实测推理 token 不变）。
        options['extra_body'] = {'thinking': {'type': 'disabled'}}
    return ChatOpenAI(**options)


def _combine_usage(usages: list):
    """合并多次尝试的 token 用量。失败的尝试同样花钱，不能丢。"""
    items = [u for u in usages if isinstance(u, dict)]
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    combined = {}
    for usage in items:
        for key, value in usage.items():
            if isinstance(value, int):
                combined[key] = combined.get(key, 0) + value
    return combined


def _draft_of(buf: str, schema):
    """把"已生成完的那部分"工具调用参数解析成半成品对象。

    工具调用参数是逐字吐出来的，用 parse_partial_json 能取出已完成的部分
    （例如标题已到、章节还在写）；字段还不齐或语法不完整时返回 None。
    """
    if not buf.strip():
        return None
    try:
        return schema.model_validate(parse_partial_json(buf))
    except Exception:
        return None


def _final_of(accumulated, schema):
    """从累积分片里取出完整的工具调用参数并做 Pydantic 校验。"""
    if accumulated is None:
        return None
    for call in (getattr(accumulated, 'tool_calls', None) or []):
        args = call.get('args')
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                continue
        try:
            return schema.model_validate(args)
        except ValidationError:
            continue
    return None


async def structured_call(
    schema, system: str, user: str, model: str | None = None, attempts: int = 2,
    disable_thinking: bool = False, on_draft=None, draft_interval: float = 1.0,
):
    """结构化调用，流式获取 + 解析失败时重试。

    为什么用 bind_tools + astream 而不是 with_structured_output：
    `with_structured_output(include_raw=True)` 在流式下**拿不到 usage**（分片里没有 raw），
    而成本统计依赖它。手动累积分片可以同时得到三样东西：渐进可显示的半成品、
    完整结果、以及 usage_metadata。

    on_draft(partial_or_None)：每次拿到新的半成品时回调；传 None 表示清空
    （重试时会先清空，避免两次尝试的内容混在一起）。
    返回 (Pydantic 对象, usage)，usage 为各次尝试的合计。
    """
    usages = []
    messages = [('system', system), ('human', user)]
    for _ in range(max(1, attempts)):
        if on_draft:
            on_draft(None)
        llm = new_llm(model, disable_thinking=disable_thinking).bind_tools(
            [schema], tool_choice=schema.__name__)
        accumulated = None
        buf = ''                 # 增量累积工具调用参数，避免每次重新拼接全部分片
        # 用 None 而不是 0.0：monotonic() 是开机以来的秒数，开机不足一个
        # draft_interval 时 "now - 0.0" 会一直小于阈值，草稿永远不触发。
        last_emit = None
        last_fingerprint = None
        async for chunk in llm.astream(messages):
            accumulated = chunk if accumulated is None else accumulated + chunk
            for piece in (getattr(chunk, 'tool_call_chunks', None) or []):
                if isinstance(piece, dict) and piece.get('args'):
                    buf += piece['args']
            if on_draft is None:
                continue
            now = time.monotonic()
            if last_emit is not None and now - last_emit < draft_interval:
                continue
            # 关键：无论这次解析成不成功都要推进节流时间戳。
            # 否则字段还没凑齐时会退化成"每个分片都重试一次"，
            # 对不断增长的缓冲区反复做解析与校验，O(n²) 会把事件循环堵死。
            last_emit = now
            draft = _draft_of(buf, schema)
            if draft is None:
                continue
            # 内容没变化就别重复推，省带宽也省前端重绘
            fingerprint = len(draft.model_dump_json())
            if fingerprint == last_fingerprint:
                continue
            last_fingerprint = fingerprint
            on_draft(draft)
        usages.append(getattr(accumulated, 'usage_metadata', None))
        parsed = _final_of(accumulated, schema)
        if parsed is not None:
            if on_draft:
                on_draft(parsed)
            return parsed, _combine_usage(usages)
    raise RuntimeError('模型结构化输出失败；检查 tool calling 支持及 schema')


async def chat_reply(system: str, messages: list[dict], model: str | None = None,
                     on_delta=None) -> str:
    """纯文本对话：不检索、不结构化。on_delta 逐段回传增量文本，做打字机效果。

    闲聊/一般性问答不需要推理，这里直接关掉思考——快且便宜，正是本路径的价值。
    """
    llm = new_llm(model, disable_thinking=True)
    conversation = [('system', system)] + [
        (m['role'], m['content']) for m in messages]
    parts = []
    async for chunk in llm.astream(conversation):
        text = chunk.content
        if isinstance(text, str) and text:
            parts.append(text)
            if on_delta:
                on_delta(text)
    return ''.join(parts).strip()


def new_tavily():
    return AsyncTavilyClient(api_key=settings.tavily_key)


async def search_query(query: str):
    return await new_tavily().search(
        query=query,
        search_depth='basic',
        max_results=settings.max_pages,
        include_answer=False,
        include_raw_content='markdown',
        auto_parameters=False,
        include_usage=True,
        timeout=30,
    )


async def extract_urls(urls: list[str]):
    return await new_tavily().extract(
        urls=urls,
        extract_depth='basic',
        format='markdown',
        timeout=30,
    )
