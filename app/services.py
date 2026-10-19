import asyncio
import re
import json
import time
import ipaddress
from urllib.parse import urlsplit, urlunsplit
from pydantic import ValidationError
from langchain_core.utils.json import parse_partial_json
from langchain_openai import ChatOpenAI
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


# 只保留文字/数字：标点、空白、emoji 全部去掉，"你好！" 与 "谢谢~" 才判得出来。
_NORMALIZE = re.compile(r'[^\w]+', re.UNICODE)

# 能独立构成一整句寒暄/确认/致谢/告别的词。
# 互为前缀的词必须长的在前（'thanks' 在 'thank' 前、'okay' 在 'ok' 前），
# 否则短词会先把长词的开头吃掉。
_SMALL_TALK_TOKENS = ('thankyou', '早上好', '中午好', '下午好', '晚上好',
                      'hello', 'thanks', '哈喽', '在不在', '你好', '您好',
                      '谢谢', '多谢', '感谢', '谢了', 'thank', 'thx',
                      '好的', '收到', '明白', '知道', 'okay', '嗯嗯',
                      '再见', '拜拜', 'bye', '早安', '晚安', '在吗', '在么',
                      '哈哈', '嘿嘿', 'ok', '3q', '88', 'hi', 'hey',
                      '嗯', '哦', '噢', '啦', '啊', '呀', '吧', '呢', '哈',
                      '嘛', '喔', '了', '好')

# 超过这个长度一律交给模型：长句里出现寒暄词（"谢谢，帮我查一下…"）不代表它是寒暄。
_SMALL_TALK_MAX_CHARS = 12


def is_small_talk(text: str) -> bool:
    """是不是"纯寒暄/确认"，本地判、不花模型调用。

    只在**极其保守**的前提下返回 True：整句能被已知寒暄词完全吃掉，且长度很短。
    判不准就返回 False 走模型——误判成寒暄会让真正的任务被当成闲聊，代价远大于
    多花一次调用，所以这里宁可漏判。
    """
    normalized = _NORMALIZE.sub('', text).lower()
    if not normalized or len(normalized) > _SMALL_TALK_MAX_CHARS:
        return False
    rest = normalized
    while rest:
        for token in _SMALL_TALK_TOKENS:
            if rest.startswith(token):
                rest = rest[len(token):]
                break
            if rest.endswith(token):
                rest = rest[:-len(token)]
                break
        else:
            return False          # 剩下的不认识了，交给模型
    return True


# 明确表示"停下"的说法。只做**整句完全匹配**，不做模糊包含：
# "取消订单"这种是正常话题，不能被当成控制指令。
_STOP_COMMANDS = {
    '暂停', '暂停一下', '先暂停', '停止', '停一下', '停下', '先停一下', '先停',
    '别搜了', '不要搜了', '不用搜了', '取消', '终止', '中断', '够了',
    '停', 'stop', 'pause', 'cancel', 'abort',
}


def is_stop_command(text: str) -> bool:
    """这句话是不是在叫停当前流程。

    客户端一旦把它当普通任务发出去，就会被拿去检索/成稿——用户想停下，结果
    反而启动了最重的一条链路。所以要在建任务之前拦下来。
    """
    return _NORMALIZE.sub('', text).lower() in _STOP_COMMANDS


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


class AttemptTimeout(RuntimeError):
    """单次模型调用超时：一次尝试没能在上限内返回完整结果。

    safe_message 是显式标记：任务失败时 run_task 默认只透出异常类型名
    （避免把 SDK 原文里的密钥/内网地址写进库），这一类自写提示可以透出。
    """

    def __init__(self, seconds: float) -> None:
        super().__init__(f'模型调用超时：单次尝试超过 {seconds:.0f}s 仍未返回完整结果')
        self.safe_message = f'模型单次尝试超时，上限 {seconds:.0f}s'


async def structured_call(
    schema, system: str, user: str, model: str | None = None, attempts: int = 2,
    disable_thinking: bool = False, on_draft=None, draft_interval: float = 1.0,
    attempt_timeout: float | None = None,
):
    """结构化调用，流式获取 + 解析失败时重试。

    为什么用 bind_tools + astream 而不是 with_structured_output：
    `with_structured_output(include_raw=True)` 在流式下**拿不到 usage**（分片里没有 raw），
    而成本统计依赖它。手动累积分片可以同时得到三样东西：渐进可显示的半成品、
    完整结果、以及 usage_metadata。

    on_draft(partial_or_None)：每次拿到新的半成品时回调；传 None 表示清空
    （重试时会先清空，避免两次尝试的内容混在一起）。
    attempt_timeout：单次尝试的硬超时（秒），默认取 settings.llm_attempt_timeout；
    传 0 或负数表示不限制。超时直接失败、不重试——一次尝试已经烧掉整个预算，
    再试一次只会让耗时翻倍。
    返回 (Pydantic 对象, usage)，usage 为各次尝试的合计。
    """
    timeout = (settings.llm_attempt_timeout
               if attempt_timeout is None else attempt_timeout)
    timeout = timeout if timeout and timeout > 0 else None

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
        timed_out = False
        try:
            # asyncio.timeout(None) 表示不限制；外部取消（用户点停止）抛的是
            # CancelledError，不会被这里翻译成超时。
            async with asyncio.timeout(timeout):
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
        except TimeoutError:
            timed_out = True
        usages.append(getattr(accumulated, 'usage_metadata', None))
        if timed_out:
            raise AttemptTimeout(timeout)
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


async def search_query(query: str):
    """按当前配置的检索后端搜索。

    后端固定在 DeepSeek 官方 Search API（见 app/search.py）。
    """
    from app import search as backend
    return await backend.search(query)


async def extract_urls(urls: list[str]):
    """抓取正文（Markdown），走 DeepSeek 官方 Fetch API。"""
    from app import search as backend
    return await backend.fetch(urls)
