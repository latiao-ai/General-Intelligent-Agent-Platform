from typing import Literal
from typing_extensions import TypedDict
from pydantic import BaseModel, Field

class PlanStep(BaseModel):
    """编排计划中的一步：调用哪个工具、传什么参数。"""
    tool: str = Field(min_length=1)
    args: dict = Field(default_factory=dict)
    why: str = ''


class ExecutionPlan(BaseModel):
    """规划器输出：按顺序执行的步骤序列。

    tool 用自由字符串而不是 Literal：MCP 工具是运行时发现的、名字无法写死，
    执行前会按真实工具清单校验并丢弃未知工具。
    """
    steps: list[PlanStep] = Field(min_length=1, max_length=8)
    summary: str = ''
    # 本轮用户透露的个人信息，没有就留空。与旧版解析阶段的行为保持一致。
    profile_updates: dict[str, str] = Field(default_factory=dict)

class Paragraph(BaseModel):
    text: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)

class Section(BaseModel):
    heading: str = Field(min_length=1)
    paragraphs: list[Paragraph] = Field(min_length=1)

class TableRow(BaseModel):
    cells: list[str] = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)

class ComparisonTable(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[TableRow] = Field(default_factory=list)

class Report(BaseModel):
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    summary_source_ids: list[str] = Field(min_length=1)
    sections: list[Section] = Field(min_length=1)
    comparison_table: ComparisonTable = Field(default_factory=ComparisonTable)
    limitations: list[str] = Field(default_factory=list)

class AgentState(TypedDict, total=False):
    task_id: str
    user_input: str
    # 编排计划：{'steps': [{'tool','args','why'}], 'summary': str}
    plan: dict
    # 执行游标：下一步要跑的是 plan['steps'] 里的第几步。
    step_index: int
    # 上一步的中文名，执行器用它做耗时行的标签。
    step_label: str
    documents: list[dict]
    report: dict
    # 直接回答时的纯文本结果
    answer: str
    artifact: dict
    validation: dict
    deployment: dict
    # MCP 工具返回的原始结果（按调用顺序）
    mcp_calls: list[dict]
    errors: list[dict]
    usage: list[dict]
    status: str
    # 各节点耗时，由执行器写入（见 app/main.py）。
    timings: list[dict]
    elapsed_seconds: float
    # 多轮对话历史 [{'role','content'}]，供规划阶段理解指代型追加要求。
    history: list[dict]
