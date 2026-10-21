from typing import Any, Literal
from typing_extensions import TypedDict
from pydantic import BaseModel, Field

class InputBinding(BaseModel):
    """步骤输入的一种绑定方式（文档 §6.2）。

    只支持两种明确形态，**不解析任意表达式**：
    - literal：字面量，直接作为参数值
    - ref：引用前序步骤的输出，按受限 JSON Pointer 取值
    """
    literal: Any = None
    ref: dict | None = None          # {'step_id': 's1', 'pointer': '/artifact_id'}


class PlanStep(BaseModel):
    """编排计划中的一步。

    step_id 可以省略（服务端按顺序补 s1、s2…）；depends_on 指向前序步骤，
    执行前会校验引用合法且不构成环。

    `compensate` 是**可选的补偿动作**（文档 §8.4）：本步骤及其后续失败时，
    系统按逆序调用它撤销已经发生的副作用。它是独立动作——有单独的工具、
    权限、账本记录，不等同于"取消任务"。
    """
    tool: str = Field(min_length=1)
    args: dict = Field(default_factory=dict)
    why: str = ''
    step_id: str = ''
    depends_on: list[str] = Field(default_factory=list)
    # {'tool': 'mcp__delete_site', 'args': {'site_id': {'ref': {...}}}}
    compensate: dict | None = None


class ExecutionPlan(BaseModel):
    """规划器输出：按顺序执行的步骤序列。

    tool 用自由字符串而不是 Literal：MCP 工具是运行时发现的、名字无法写死，
    执行前会按真实工具清单校验并丢弃未知工具。
    """
    steps: list[PlanStep] = Field(min_length=1, max_length=8)
    summary: str = ''
    # 本轮用户透露的个人信息，没有就留空。与旧版解析阶段的行为保持一致。
    profile_updates: dict[str, str] = Field(default_factory=dict)

class SessionMemory(BaseModel):
    """会话记忆：更早对话的压缩结果。人可读，也可被用户直接编辑。"""
    summary: str = ''
    facts: dict[str, str] = Field(default_factory=dict)




class AgentState(TypedDict, total=False):
    task_id: str
    user_input: str
    # 编排计划：{'steps': [{'tool','args','why'}], 'summary': str}
    plan: dict
    # 执行游标：下一步要跑的是 plan['steps'] 里的第几步。
    step_index: int
    # 上一步的中文名，执行器用它做耗时行的标签。
    step_label: str
    # 已执行步骤的输出，按 step_id 索引，供后续步骤的 ref 绑定取值（文档 §6.2）
    step_outputs: dict
    documents: list[dict]
    report: dict
    # 直接回答时的纯文本结果
    answer: str
    artifact: dict
    # 同一会话里上一轮已生成的产物。用户单独说"部署到服务器"时，
    # 这一轮自己没有产出产物，就用它作为部署对象。
    prior_artifact: dict
    # 同一会话里上一轮已取得的正文来源（status=success）。用户说"把上面的内容整理成
    # HTML"时，这一轮自己没有检索，得靠它把材料接过来。
    prior_documents: list[dict]
    validation: dict
    deployment: dict
    # MCP 工具返回的原始结果（按调用顺序）
    mcp_calls: list[dict]
    # 「资料转 HTML」产出的页面信息（标题/份数）
    sources_page: dict
    errors: list[dict]
    usage: list[dict]
    # 终态（**通用**状态：succeeded / failed / needs_reconcile …）。
    # `answered` / `local_ready` 这类展示词由 app/outcome.py 派生，不入状态。
    status: str
    # 本轮产出的结构化记录：{kind, produced[], effects[], answered, unverified}
    outcome: dict
    # 失败原因（给人看的一句话）；与 status 分开：状态是词汇，原因是文案
    failure: str
    # 已执行/已失败的回滚明细
    compensations: list[dict]
    # 账本发现有"结果未定"的动作时置位：禁止自动重放
    needs_reconciliation: bool
    # 续跑用：沿用已保存的计划，不重新规划
    resume_plan: dict
    run_id: str
    # 各节点耗时，由执行器写入（见 app/main.py）。
    timings: list[dict]
    elapsed_seconds: float
    # 多轮对话历史 [{'role','content'}]，供规划阶段理解指代型追加要求。
    history: list[dict]
    # 更早对话的压缩结果 {'summary','facts','upto'}，由 context 装配时注入。
    session_memory: dict
