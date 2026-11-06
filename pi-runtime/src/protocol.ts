/**
 * 桥接协议类型定义（与 Python 侧 agent/pi_protocol.py 保持一致）。
 *
 * 每行一个 UTF-8 JSON；stdout 只传协议，日志写 stderr。
 * 所有消息统一携带 protocol_version、execution_id、type。
 */

export const PROTOCOL_VERSION = 1;

/** 通用消息基座 */
export interface BaseMessage {
  protocol_version: number;
  execution_id: string;
  type: string;
}

// ---- Node → Python ----

/** 启动完成，声明协议版本 */
export interface ReadyMessage extends BaseMessage {
  type: 'ready';
}

/** 文本增量 / 进度 / 完成提示（展示用） */
export interface EventMessage extends BaseMessage {
  type: 'event';
  kind: 'text_delta' | 'progress' | 'stage';
  /** text_delta 时的文本增量 */
  delta?: string;
  /** progress/stage 时的说明 */
  label?: string;
  status?: string;
}

/** 工具调用请求：Pi 要调用一个工具，等待 Python 回执 */
export interface ToolCallMessage extends BaseMessage {
  type: 'tool_call';
  call_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
}

/** 本轮终结结果 */
export interface FinishedMessage extends BaseMessage {
  type: 'finished';
  /** answered | cancelled | failed | waiting —— 只表示本轮结束，不自动代表业务成功。
   *  waiting：远端已受理、结果未定，宿主确认等待事实落库后结束本轮、释放进程。 */
  outcome: 'answered' | 'cancelled' | 'failed' | 'waiting';
  /** outcome=answered 时的最终答复文本 */
  answer?: string;
  /** outcome=failed 时的错误说明 */
  error?: string;
}

/** 桥接或引擎错误 */
export interface ErrorMessage extends BaseMessage {
  type: 'error';
  error: string;
}

// ---- Python → Node ----

/** 宿主传入的工具声明：name + 描述 + 完整 JSON Schema 参数。
 *
 * `parameters` 是完整的 JSON Schema object（含 type/properties/required），
 * 直接来自能力包 `Tool.args`。空的 tools 数组表示「本轮没有任何工具」，
 * Node 不得回退到内置 echo/add。
 */
export interface HostToolDecl {
  name: string;
  description: string;
  /** 完整 JSON Schema（type=object，可含 properties/required/嵌套对象/数组）。 */
  parameters: Record<string, unknown>;
}

/** 开始一轮：输入、上下文、允许的工具目录、运行限制 */
export interface StartMessage extends BaseMessage {
  type: 'start';
  user_input: string;
  system_prompt?: string;
  history?: Array<{ role: 'user' | 'assistant'; content: string }>;
  tools: HostToolDecl[];
  model: {
    id: string;
    base_url: string;
    provider?: string;
    api?: string;
  };
  /** 凭据经初始化通道传入，不放命令行参数、不写协议日志 */
  api_key?: string;
  /** 模型运行超时（毫秒） */
  model_timeout_ms?: number;
  /**
   * 受控模型脚本（测试）：非空时 Node 不走真实模型，按脚本逐轮产出确定性的
   * 工具调用/答复，用于无外部凭据下经真实 Node 端到端验证恢复链路。
   */
  mock?: Array<{
    type: 'answer';
    text: string;
    /** 若设置，受控模型必须在上文 toolResult 中看到该标记才产出答复，
     *  且答复文本派生自真实 toolResult 内容（验证结果确实被消费）。 */
    expect?: string;
  } | {
    type: 'tool';
    calls: Array<{ id: string; name: string; arguments: Record<string, unknown> }>;
  }>;
  /**
   * 恢复上下文（第七阶段）：携带已提交的完整模型消息（含 assistant/toolCall/
   * toolResult 配对）。有 resume 时 Node 用它**重建** session 上下文，不再
   * 追加原始用户输入——避免恢复后把同一句用户输入再发一遍。
   */
  resume?: {
    messages: Array<Record<string, unknown>>;
    /** 已提交的检查点序号：恢复时 Node 从它继续，不从 0 重新开始。 */
    checkpoint_seq?: number;
    /** 恢复模式（第七阶段收口）：
     *  - 'admit'：继续原 pending call（重派发，宿主按账本 replay 或执行）；
     *  - 'deliver'：交付已提交结果（重派发原 call_id，宿主 replay 不重执行）。
     *  缺省为 undefined（首次执行，正常 pushUser + 请求模型）。 */
    mode?: 'admit' | 'deliver';
    /** 待重派发的原工具调用（含原 call_id / tool_name / arguments）。 */
    pending_calls?: Array<{
      call_id: string;
      tool_name: string;
      arguments: Record<string, unknown>;
    }>;
    /**
     * 已受理外部任务完成后、宿主读回并装配的最终结果（第八阶段收口第 3 点）。
     * Node 恢复时**直接注入** toolResult（不重新派发工具），再继续模型推理。
     */
    deliver_results?: Array<{
      call_id: string;
      tool_name: string;
      result: unknown;
      is_error: boolean;
    }>;
  };
}

/** 一次模型请求的准入请求：Node 在真正 stream() 前发，等待 allow/deny。 */
export interface ModelRequestMessage extends BaseMessage {
  type: 'model_request';
  request_id: string;
}

/** 版本化上下文导出（Node → Python）：完整模型轮次结束 / 工具候选确定时发。 */
export interface CheckpointMessage extends BaseMessage {
  type: 'checkpoint';
  checkpoint_seq: number;
  /** 完整模型消息（assistant / toolCall / toolResult 配对）。 */
  messages: Array<Record<string, unknown>>;
  /** 待处理工具调用（已确定候选、尚未准入/完成）。 */
  pending_calls: Array<Record<string, unknown>>;
  /** 最终答复候选（answered 前持久化，供恢复 resume_finish 补收尾）。 */
  final_answer?: string;
}

/** 检查点持久化确认（Python → Node）。 */
export interface CheckpointAckMessage extends BaseMessage {
  type: 'checkpoint_ack';
  checkpoint_seq: number;
  ok: boolean;
}

/** 模型请求准入应答（Python → Node）。 */
export interface ModelAdmitMessage extends BaseMessage {
  type: 'model_admit';
  request_id: string;
  /** allow=1 才可真正请求模型；否则 Node 不得 stream。 */
  allow: boolean;
  /** deny 时的原因。 */
  reason?: string;
}

/** 模型用量结算上报（Node → Python）。 */
export interface ModelUsageMessage extends BaseMessage {
  type: 'model_usage';
  request_id: string;
  outcome: 'succeeded' | 'failed' | 'cancelled';
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    cache_read_tokens?: number;
    cache_write_tokens?: number;
    total_tokens?: number;
  };
}

/** 模型用量结算确认（Python → Node）。 */
export interface ModelUsageAckMessage extends BaseMessage {
  type: 'model_usage_ack';
  request_id: string;
  /** 结算是否成功（第七阶段：失败时 Node 停止后续推理）。 */
  ok?: boolean;
}

/** 工具回执：同一 call_id 的成功结果或工具错误 */
export interface ToolResultMessage extends BaseMessage {
  type: 'tool_result';
  call_id: string;
  ok: boolean;
  result?: unknown;
  error?: string;
  /** 远端已受理、结果未定（第八阶段）：Node 不得把受理当最终结果继续推理，
   *  应结束本轮并释放进程等待唤醒。 */
  accepted?: boolean;
  /** 远端任务身份（accepted 时随回执带回，供恢复关联）。 */
  remote_task_id?: string;
}

/** 停止执行 / 关闭进程 */
export interface ControlMessage extends BaseMessage {
  type: 'cancel' | 'shutdown';
}

export type InboundMessage =
  | StartMessage
  | ToolResultMessage
  | ModelAdmitMessage
  | ModelUsageAckMessage
  | CheckpointAckMessage
  | ControlMessage;
export type OutboundMessage =
  | ReadyMessage
  | EventMessage
  | ToolCallMessage
  | ModelRequestMessage
  | ModelUsageMessage
  | CheckpointMessage
  | FinishedMessage
  | ErrorMessage;
