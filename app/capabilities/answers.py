"""answers 能力包：在聊天里回答（直答 / 带来源作答）。"""

from app import context, profile
from app.capabilities.base import (ToolError, _select_sources,
                                   tool_results_text, usable_documents)
from app.capabilities.registry import tool
from app.events import action, answer_delta, emit
from app.services import chat_reply

# 能力包名，也进 tool_key（local:answers:<动作>）
CAPABILITY = 'answers'


# 本能力包自带的规划规则：什么时候该在聊天里作答、什么时候不必检索。
PROMPT_RULES = (
    '【作答】result_locus=chat，而且只是想问明白、不要文件时：'
    '模型有把握就 answer_directly；要最新或外部事实时先 web_search，'
    '再 answer_with_sources。不要为这种需求生成 HTML。'
    '名词解释、原理、常识、做法建议有把握就直接答，不要为了保险去检索。'
    '只有涉及具体数字/价格/日期/版本/排名、明确要"最新/现在/今年"、'
    '提到不确定的新名词或圈内事件、或者用户明说要查资料时，才必须检索。'
    '拿不准时先直接答，并在回答里说明"未联网核实"。'
    '寒暄、确认一律 answer_directly。'
    'result_locus=platform_state 或 platform_change 时，不要用网页搜索或'
    'answer_with_sources 充数。没有对应的平台工具时，只用 answer_directly '
    '说明看不到或做不到。'
)


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


@tool(name='answer_directly', label='直接回答', effect='read',
      key='local:answers:direct',
      description='不检索、不出文件，用模型自己的知识回答。概念解释、原理、常识、'
                  '闲聊、对上一轮的追问都用它——不要为了保险先跑一次检索。',
      args={'type': 'object',
            'properties': {'question': {'type': 'string'}}})
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


# 只依据资料作答，**不加任何引用标记**：检索接口返回什么就用什么，
# 不再代它发明 [s1]/[s2] 编号，也不再在正文后面追加一份"参考来源"清单。
ANSWER_WITH_SOURCES_SYSTEM = (
    '你是一个严谨的中文研究助手。只依据给定资料回答用户问题，不得使用资料之外的事实。'
    '资料不足以回答的部分要明确说明；资料互相冲突时把差异讲出来，不要强行统一。'
    '输出用自然的段落或短列表，不要写"根据资料"这类空话，也不要添加引用编号或来源清单。'
)


@tool(name='answer_with_sources', label='联网作答', effect='read',
      key='local:answers:with_sources',
      description='基于已检索到的正文在聊天里回答问题（原样作答，不追加引用清单）。'
                  '用户想"问个明白"但没要文件时用它；'
                  '必须在 web_search / fetch_pages 之后调用。',
      args={'type': 'object',
            'properties': {'question': {'type': 'string'}}})
async def _answer_with_sources(args: dict, state: dict) -> dict:
    """联网检索后在聊天里作答（原样输出，不追加引用清单）。

    这是"直接回答"和"出一份 HTML 报告"之间的那条路：用户只想问个明白、
    不想为了一份文档等上一分钟。
    """
    question = str(args.get('question') or state.get('user_input') or '')
    documents, origin = usable_documents(state)
    if not documents:
        raise ToolError('还没有可用正文：先用 web_search 检索取得正文')
    if origin:
        emit(action('沿用上一轮的正文', f'{len(documents)} 个来源', '♻️'))
    selected, _ = _select_sources(state, documents)
    emit(action('正在依据资料作答',
                f'{len(selected)} 个来源 · {sum(len(s["text"]) for s in selected)} 字符',
                '📝'))
    # 资料只按标题区分，**不编号**：给了 [s1] 这种标签，模型就会把它抄进回答，
    # 而那正是本次要去掉的"额外引用来源"。
    material = '\n\n'.join(
        f'《{item["title"]}》\n{item["text"]}' for item in selected)
    results = tool_results_text(state)
    if results:
        material = f'{results}\n\n{material}'
    # 截断声明必须**先拼进 material，再构造 messages**。
    # 原先是先建 messages、后往 material 追加——模型收到的还是"干净"的片段，
    # 于是把"前 3000 字"当全文，输出"资料已完整覆盖"。这正是文档 §11 点名
    # 要避免的失败模式，而验收清单里那条 ✅ 名不副实。
    truncated_notes = [f'《{item["title"]}》原文 {item["chars_full"]} 字，'
                       f'仅取前 {len(item["text"])} 字'
                       for item in selected if item.get('truncated')]
    if truncated_notes:
        material += ('\n\n[注意] 以下资料被截断，回答时不要把这些片段'
                     '当成全文，必要时说明：\n' + '\n'.join(truncated_notes))
    # 多轮追问靠这里：此前这条路径完全不带历史，问"那第二种呢"就断了线索
    messages = context.chat_messages(
        state.get('history'), f'问题：{question}\n\n资料：\n{material}',
        purpose='answer')
    body = await chat_reply(
        profile.with_profile(
            context.system_with_memory(ANSWER_WITH_SOURCES_SYSTEM,
                                       state.get('session_memory'))),
        messages, on_delta=lambda text: emit(answer_delta(text)))

    # 原样返回模型的回答：不再追加"参考来源"清单。
    # 来源本身仍是真实回执（documents 里的 url），只是不再由平台代贴一遍——
    # 检索接口返回什么内容，这里就呈现什么内容。
    return {'answer': body.strip(), 'status': 'answered'}
