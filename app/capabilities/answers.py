"""answers 能力包：在聊天里回答（直答 / 带来源作答）。"""
import json

from app import context, profile, sources
from app.capabilities.base import (ToolError, _select_sources,
                                   tool_results_text, usable_documents)
from app.events import action, answer_delta, emit
from app.services import chat_reply


ANSWER_SYSTEM = (
    '你是一个简洁、可靠的中文助手。直接回答用户的问题，不要编造事实。'
    '本轮没有做任何联网检索，你只有自己已有的知识。'
    '如果问题依赖最新或外部事实（版本号、价格、日期、新闻）而你不确定，'
    '就明确说明你无法确认，并建议联网检索。'
)


ANSWER_USING_TOOL_RESULTS_SYSTEM = (
    '你是一个简洁、可靠的中文助手。用户的问题下面已经附上了本轮工具返回的真实数据，'
    '必须基于这些数据回答：该数就数、该列就列，直接给结论。'
    '不要说"我无法访问服务器/没有联网/你没提供给我"——数据就在下面。'
    '数据里确实没有的信息，就明确说那部分没有，不要编造。'
    # 这条是真踩过的坑：list_sites 带 keyword 过滤时只返回匹配项，
    # 模型把"过滤后 1 条"当成"服务器上只剩 1 个"，还把自己刚删掉的站点列成了幸存者。
    '特别注意：如果某次调用带了过滤/搜索类参数（如 keyword），它的返回只是匹配结果，'
    '不是全量清单。此时不要报"还剩几个"、也不要据此罗列剩余条目，'
    '应当说明需要重新查询一次完整列表。只有不带过滤参数的调用结果，才代表全量。'
)


async def _answer_directly(args: dict, state: dict) -> dict:
    question = str(args.get('question') or state.get('user_input') or '')
    results = tool_results_text(state)
    if results:
        system = ANSWER_USING_TOOL_RESULTS_SYSTEM
        prefix = f'工具返回的数据：\n{results}\n\n问题：{question}'
        emit(action('直接回答', '基于本轮工具返回的数据', '💬'))
    else:
        system = ANSWER_SYSTEM
        prefix = f'问题：{question}'
        emit(action('直接回答', '本轮无需联网检索', '💬'))
    messages = context.chat_messages(state.get('history'), prefix,
                                     purpose='answer')
    answer = await chat_reply(
        profile.with_profile(context.system_with_memory(system, state.get('session_memory'))),
        messages, on_delta=lambda text: emit(answer_delta(text)))
    return {'answer': answer, 'status': 'answered'}


ANSWER_WITH_SOURCES_SYSTEM = (
    '你是一个严谨的中文研究助手。只依据给定资料回答用户问题，不得使用资料之外的事实。'
    '关键结论后面用 [s1]、[s2] 这样的编号标注它来自哪条资料，可一句多条。'
    '资料不足以回答的部分要明确说明；资料互相冲突时把差异讲出来，不要强行统一。'
    '输出用自然的段落或短列表，不要写"根据资料"这类空话。'
)


def _source_line(doc: dict) -> str:
    return f'[{doc["source_id"]}] {doc.get("title") or doc["url"]} — {doc["url"]}'


async def _answer_with_sources(args: dict, state: dict) -> dict:
    """联网检索后在聊天里作答，并附上来源。

    这是"直接回答"和"出一份 HTML 报告"之间的那条路：用户只想问个明白、
    不想为了一份文档等上一分钟。
    """
    question = str(args.get('question') or state.get('user_input') or '')
    documents, origin = usable_documents(state)
    if not documents:
        raise ToolError('还没有可用正文，请先调用 web_search，再用 fetch_pages 抓正文')
    if origin:
        emit(action('沿用上一轮的正文', f'{len(documents)} 个来源', '♻️'))
    selected, _ = _select_sources(state, documents)
    by_id = {d['source_id']: d for d in documents if d.get('source_id')}
    emit(action('正在依据资料作答',
                f'{len(selected)} 个来源 · {sum(len(s["text"]) for s in selected)} 字符',
                '📝'))
    material = '\n\n'.join(
        f'[{item["source_id"]}] {item["title"]}\n{item["text"]}' for item in selected)
    results = tool_results_text(state)
    if results:
        material = f'{results}\n\n{material}'
    # 多轮追问靠这里：此前这条路径完全不带历史，问"那第二种呢"就断了线索
    messages = context.chat_messages(
        state.get('history'), f'问题：{question}\n\n资料：\n{material}',
        purpose='answer')
    truncated_notes = [f'[{item["source_id"]}] 原文 {item["chars_full"]} 字，'
                       f'仅取前 {len(item["text"])} 字'
                       for item in selected if item.get('truncated')]
    if truncated_notes:
        material += ('\n\n[注意] 以下资料被截断，回答时不要把这些片段'
                     '当成全文，必要时说明：\n' + '\n'.join(truncated_notes))
    body = await chat_reply(
        profile.with_profile(
            context.system_with_memory(ANSWER_WITH_SOURCES_SYSTEM,
                                       state.get('session_memory'))),
        messages, on_delta=lambda text: emit(answer_delta(text)))

    # 只列真正被引用的来源；一条都没引用时退回全部，免得"附来源"变成空话。
    cited = [item for item in selected
             if f'[{item["source_id"]}]' in body] or selected
    lines = [_source_line(by_id.get(item['source_id'], item)) for item in cited]
    answer = body.rstrip() + '\n\n参考来源：\n' + '\n'.join(f'- {line}' for line in lines)
    return {'answer': answer, 'status': 'answered'}
