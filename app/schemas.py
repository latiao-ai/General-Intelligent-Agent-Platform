from typing import Literal
from typing_extensions import TypedDict
from pydantic import BaseModel, Field

class Plan(BaseModel):
    mode: Literal['topic', 'urls'] = 'topic'
    topic: str = Field(min_length=1, max_length=500)
    queries: list[str] = Field(default_factory=list, max_length=2)
    urls: list[str] = Field(default_factory=list, max_length=5)
    required_sections: list[str] = Field(default_factory=list, max_length=8)
    language: str = 'zh-CN'
    # 用户是否明确要求把生成出来的报告本身部署到服务器。
    # 故意设为必填：带默认值时模型会直接省略该字段，被默认值静默兜成 False，
    # 导致"生成后部署到服务器"这类明确诉求永远识别不出来。
    auto_deploy: bool

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
    plan: dict
    documents: list[dict]
    research_round: int
    searched: bool
    report: dict
    artifact: dict
    validation: dict
    deployment: dict
    errors: list[dict]
    usage: list[dict]
    status: str
    # 各节点耗时，由执行器写入（见 app/main.py）。
    timings: list[dict]
    elapsed_seconds: float
    # 多轮对话历史 [{'role','content'}]，供解析阶段理解指代型追加要求。
    history: list[dict]
