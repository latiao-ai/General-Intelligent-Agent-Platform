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
  /** answered | cancelled | failed —— 只表示本轮结束，不自动代表业务成功 */
  outcome: 'answered' | 'cancelled' | 'failed';
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

/** 开始一轮：输入、上下文、允许的工具目录、运行限制 */
export interface StartMessage extends BaseMessage {
  type: 'start';
  user_input: string;
  system_prompt?: string;
  history?: Array<{ role: 'user' | 'assistant'; content: string }>;
  tools: Array<{ name: string; description: string; parameters: Record<string, string> }>;
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
}

/** 工具回执：同一 call_id 的成功结果或工具错误 */
export interface ToolResultMessage extends BaseMessage {
  type: 'tool_result';
  call_id: string;
  ok: boolean;
  result?: unknown;
  error?: string;
}

/** 停止执行 / 关闭进程 */
export interface ControlMessage extends BaseMessage {
  type: 'cancel' | 'shutdown';
}

export type InboundMessage = StartMessage | ToolResultMessage | ControlMessage;
export type OutboundMessage =
  | ReadyMessage
  | EventMessage
  | ToolCallMessage
  | FinishedMessage
  | ErrorMessage;
