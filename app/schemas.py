from typing import Any, ClassVar, Literal
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


class CompletionCriterion(BaseModel):
    """一条"任务算不算做完"的可验证条件（文档 observe-decide §3.2、§11）。

    分成两类，永远不要混：
    - **程序可核对**（`artifact_exists` / `file_written` / `deployment_receipt` /
      `answer_present` / `tool_succeeded`）：证据来自真实回执与文件系统；
    - **需要模型评价**（`content_quality` / `semantic_match` / …）：程序只标
      "未检查"，不允许因为文件存在就宣称合格。

    模型可以"提出"条件，但**核对**由执行器的验证器做，模型自述不算证据。
    """
    kind: str = Field(min_length=1)
    target: str = ''
    detail: str = ''
    # 明确标注"必须看内容才能判断"的条件，程序不会替它下结论
    requires_model: bool = False


class SegmentStrategy(BaseModel):
    """片段策略：一个执行片段结束时该做什么（文档 observe-decide §3.2）。

    首版只支持两种，且**由候选计划自己声明**——执行器不靠工具名关键词猜。
    """
    after_segment: str = 'verify'          # observe / verify
    observation_goal: str = ''             # after_segment=observe 时必填

    def model_post_init(self, __context) -> None:      # noqa: D105
        if self.after_segment not in ('observe', 'verify'):
            raise ValueError('after_segment 只能是 observe 或 verify')
        if self.after_segment == 'observe' and not self.observation_goal.strip():
            raise ValueError('声明 observe 时必须写清 observation_goal')
        if self.after_segment == 'verify':
            self.observation_goal = ''


class ExecutionPlan(BaseModel):
    """规划器输出：按顺序执行的步骤序列。

    tool 用自由字符串而不是 Literal：MCP 工具是运行时发现的、名字无法写死，
    执行前会按真实工具清单校验并丢弃未知工具。
    """
    steps: list[PlanStep] = Field(min_length=1, max_length=8)
    summary: str = ''
    # 片段策略（文档 observe-decide §3.2）：默认 verify = 直接执行到底；
    # observe = 这一小段跑完先让系统看结果再决定下面做什么。
    # 首次计划可以只包含"能确定的事"，把依赖未知结果的步骤留给观察之后。
    after_segment: str = 'verify'
    observation_goal: str = ''
    completion_criteria: list[CompletionCriterion] = Field(default_factory=list)
    # 本轮用户透露的个人信息，没有就留空。与旧版解析阶段的行为保持一致。
    profile_updates: dict[str, str] = Field(default_factory=dict)

class RevisedPlan(BaseModel):
    """运行中补充要求后的**剩余计划**（文档 harness-live-input §5 第十步）。

    与 `ExecutionPlan` 的差别：这里只描述"接下来还要做什么"，已经执行成功的
    步骤通过 `ref` 引用它们的输出，不重写、不删除。`superseded` 列出被这次
    调整作废的旧步骤 ID，保留审计。
    """
    steps: list[PlanStep] = Field(default_factory=list, max_length=8)
    summary: str = ''
    # 被作废的、原计划里尚未执行的步骤 ID
    superseded: list[str] = Field(default_factory=list)
    # 需要用户先澄清的问题；非空表示这一轮不产生可执行计划
    questions: list[str] = Field(default_factory=list)
    # 与 ExecutionPlan 相同：新片段要不要在结束时观察
    after_segment: str = 'verify'
    observation_goal: str = ''
    completion_criteria: list[CompletionCriterion] = Field(default_factory=list)


class ObservationDecision(BaseModel):
    """观察之后模型能做的**唯一**几件事（文档 observe-decide §5）。

    用判别字段 `kind` + 逐分支必备字段，而不是自由文本"反思"：
    自由文本没法校验、没法记账、也没法保证"模型没读到的东西不算看过"。
    """
    kind: str = Field(min_length=1)     # continue/revise/wait_input/wait_approval/finish/stop
    reason: str = ''                    # 简短依据（不索取隐藏推理）
    # revise：接下来这一段要执行的步骤（非空）、作废范围、片段策略
    steps: list[PlanStep] = Field(default_factory=list, max_length=8)
    superseded: list[str] = Field(default_factory=list)
    after_segment: str = 'verify'
    observation_goal: str = ''
    # wait_input：需要用户补什么
    questions: list[str] = Field(default_factory=list)
    # wait_approval：要批准的动作与范围
    approval_scope: str = ''
    # finish：逐条完成条件 + 证据引用
    criteria_evidence: dict[str, str] = Field(default_factory=dict)
    # stop：具体原因
    stop_reason: str = ''
    completion_criteria: list[CompletionCriterion] = Field(default_factory=list)

    DECISIONS: ClassVar[tuple[str, ...]] = (
        'continue', 'revise', 'wait_input', 'wait_approval', 'finish', 'stop')

    def model_post_init(self, __context) -> None:      # noqa: D105
        kind = (self.kind or '').strip()
        if kind not in self.DECISIONS:
            raise ValueError(f'未知决策 {kind!r}：只能是 {", ".join(self.DECISIONS)}')
        if kind == 'revise':
            if not self.steps:
                # 空 continue / 空 revise 都属于"无效决策"：不能靠提交空计划表示结束
                raise ValueError('revise 必须给出至少一个要执行的步骤')
            if self.after_segment not in ('observe', 'verify'):
                raise ValueError('after_segment 只能是 observe 或 verify')
            if self.after_segment == 'observe' and not self.observation_goal.strip():
                raise ValueError('after_segment=observe 时必须写 observation_goal')
        if kind == 'wait_input' and not [q for q in self.questions if q.strip()]:
            raise ValueError('wait_input 必须说明需要用户补充什么')
        if kind == 'wait_approval' and not self.approval_scope.strip():
            raise ValueError('wait_approval 必须说明要批准的动作与范围')
        if kind == 'stop' and not self.stop_reason.strip():
            raise ValueError('stop 必须给出具体原因')


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
    # ---------- 运行中补充输入（文档 harness-live-input） ----------
    # 三个版本必须分别存在：lease_epoch 在 ExecutionContext 里（不进 state），
    # 这里只放"用户要求版本"与"计划版本"。
    original_goal: str
    intent_version: int                 # 本次决策依据的要求版本（冻结）
    processed_intent_version: int       # 已被计划覆盖的要求版本
    pending_inputs: list[dict]          # 尚未纳入计划提交的补充（按 input_seq）
    intent_changed: bool                # 边界上发现版本变化，需要重新规划
    revision_count: int                 # 本次运行已经修订过几次计划
    plan_version: int                   # 当前计划版本
    # intake 节点的路由结果：plan / revise / run_step / finalize / stop
    entry_route: str
    # 修订计划时给校验器的"受信历史输出"（已成功步骤 + 其 step_id）
    trusted_outputs: dict
    # ---------- 结果观察与决策（文档 harness-observe-decide） ----------
    # 每次工具调用归一后的结果（含完整结果引用与有界摘要）
    tool_results: list[dict]
    # 程序观察到的信号与它选出的路由（程序判断，不调模型）
    observation: dict
    next_route: str
    # 结果水位：每次结果落库在同事务 +1，用来判断"决策覆盖到哪一步"
    observation_seq: int
    # 当前决策记录（持久在 run_decisions；state 里只留 id 与版本）
    decision_id: str
    decision_kind: str
    # 这次决策的触发原因：observation（结果观察）/ recovery_repair（有限修复）
    decision_reason: str
    # 模型提出的完成条件与证据（仍需程序核对）
    criteria_proposed: list[dict]
    criteria_evidence: dict
    # 观察时用到的动作快照（由 observe_result 从库里读，不靠状态猜）
    actions_snapshot: list[dict]
    # 本任务的产物目录（结果的完整 JSON 与产物文件都在它下面）
    artifact_dir: str
    # 完成条件的核对现状
    criteria_state: dict
    # 同类决策/修订的计数：user_input / observation / recovery_repair 分开记
    revision_reasons: dict
    # 连续无进展的次数（相同指纹重复出现就停）
    no_progress: int
    no_progress_limit: int
    # 无进展检测：最近几次"查询+结果"签名
    progress_seen: list[str]
    # 各节点耗时，由执行器写入（见 app/main.py）。
    timings: list[dict]
    elapsed_seconds: float
    # 多轮对话历史 [{'role','content'}]，供规划阶段理解指代型追加要求。
    history: list[dict]
    # 更早对话的压缩结果 {'summary','facts','upto'}，由 context 装配时注入。
    session_memory: dict
