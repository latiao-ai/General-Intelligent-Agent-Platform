/**
 * Pi session 的显式创建、运行与销毁。
 *
 * 第三阶段使用**内存 session**：一个 Context 对象，一次运行独占。不加载持久
 * 会话存储、不加载用户目录扩展、MCP 或文件操作能力。
 *
 * 模型端点来自宿主通过 start 消息传入（含凭据），Node 侧据此构造一个
 * OpenAI 兼容的自定义 Model。凭据只经初始化通道传递，绝不写命令行参数或日志。
 */
import { randomUUID } from 'node:crypto';
import {
  stream,
  type AssistantMessage,
  type Context,
  type Model,
  type Tool,
} from '@mariozechner/pi-ai';

export interface SessionConfig {
  systemPrompt?: string;
  tools: Tool[];
  model: {
    id: string;
    baseUrl: string;
    provider?: string;
    api?: string;
  };
  apiKey?: string;
  modelTimeoutMs?: number;
}

/**
 * 一次推理的终结状态（按安装版本 SDK 的 `stopReason` 区分，不做伪造兜底）。
 *
 * - `answered`：模型正常产出最终答复（stopReason = "stop" / "length"）；
 * - `toolUse`：模型因要调用工具而停下，等待宿主回执后继续（stopReason = "toolUse"）；
 * - `error`：模型运行失败（stopReason = "error"，含 errorMessage）；
 * - `aborted`：模型被中止（stopReason = "aborted"，通常是取消）。
 */
export type TurnEnd =
  | { kind: 'answered'; answer: string; message: AssistantMessage }
  | { kind: 'toolUse'; message: AssistantMessage }
  | { kind: 'error'; error: string; message: AssistantMessage }
  | { kind: 'aborted'; message: AssistantMessage };

/** 由宿主传入的模型配置构造一个 OpenAI 兼容的自定义 Model。 */
export function buildModel(cfg: SessionConfig['model']): Model<'openai-completions'> {
  return {
    id: cfg.id,
    name: cfg.id,
    api: (cfg.api as 'openai-completions') || 'openai-completions',
    provider: cfg.provider || 'deepseek',
    baseUrl: cfg.baseUrl,
    reasoning: false,
    input: ['text'],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: 128000,
    maxTokens: 8192,
  };
}

/** 从 assistant 消息里提取纯文本答复。 */
export function extractText(message: AssistantMessage): string {
  const parts: string[] = [];
  for (const block of message.content) {
    if (block.type === 'text') {
      parts.push(block.text);
    }
  }
  return parts.join('');
}

/** 已知的「正常停止」原因：只有 stop / length 算正常回答。
 *
 * 模型侧新增停止语义时（例如未来版本加了新的 stopReason 字符串），未知值
 * 会被 `classifyTurnEnd` 判成 error，而不是静默当作成功——这样才不会把
 * 「模型其实出错了」误报成「回答成功」。
 */
const KNOWN_STOP: ReadonlySet<string> = new Set(['stop', 'length']);

/**
 * 按停止原因把一次推理归类为 TurnEnd（不依赖 stream 的副作用，纯函数）。
 *
 * 优先级：error/aborted 优先于 toolUse，toolUse 优先于正常停止；未知停止原因
 * 一律按 error 处理，绝不伪造正常停止兜底。
 *
 * @param reason 最终消息的 stopReason（可能缺失或未知字符串）
 * @param sawToolCall 本轮是否出现过 toolcall_end（Pi 要调用工具）
 * @param streamError 显式错误消息（error 事件），优先于 stopReason
 */
export function classifyTurnEnd(
  reason: string | null | undefined,
  sawToolCall: boolean,
  streamError: string | null,
): 'answered' | 'toolUse' | 'error' | 'aborted' {
  if (streamError || reason === 'error') {
    return 'error';
  }
  if (reason === 'aborted') {
    return 'aborted';
  }
  if (reason === 'toolUse' || sawToolCall) {
    return 'toolUse';
  }
  if (reason === undefined || reason === null || !KNOWN_STOP.has(reason)) {
    return 'error';
  }
  return 'answered';
}

export class PiSession {
  private context: Context;
  private model: Model<'openai-completions'>;
  private apiKey?: string;
  private modelTimeoutMs: number;

  constructor(cfg: SessionConfig) {
    this.model = buildModel(cfg.model);
    this.apiKey = cfg.apiKey;
    this.modelTimeoutMs = cfg.modelTimeoutMs ?? 180_000;
    this.context = {
      systemPrompt: cfg.systemPrompt,
      messages: [],
      tools: cfg.tools,
    };
  }

  /** 追加用户输入（含历史时按序塞进 messages）。 */
  pushUser(text: string): void {
    this.context.messages.push({
      role: 'user',
      content: text,
      timestamp: Date.now(),
    });
  }

  /** 追加历史消息（start 时由宿主带入的多轮上下文）。 */
  pushHistory(history: Array<{ role: 'user' | 'assistant'; content: string }>): void {
    for (const h of history) {
      if (h.role === 'user') {
        this.context.messages.push({
          role: 'user',
          content: h.content,
          timestamp: Date.now(),
        });
      } else {
        // 历史里的 assistant 消息只保留文本，作为纯上下文
        this.context.messages.push({
          role: 'assistant',
          content: [{ type: 'text', text: h.content }],
          api: this.model.api,
          provider: this.model.provider,
          model: this.model.id,
          usage: {
            input: 0,
            output: 0,
            cacheRead: 0,
            cacheWrite: 0,
            totalTokens: 0,
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
          },
          stopReason: 'stop',
          timestamp: Date.now(),
        });
      }
    }
  }

  /** 追加工具回执。 */
  pushToolResult(callId: string, toolName: string, result: unknown, isError: boolean): void {
    const text = isError ? String(result ?? '工具执行失败') : JSON.stringify(result);
    this.context.messages.push({
      role: 'toolResult',
      toolCallId: callId,
      toolName,
      content: [{ type: 'text', text }],
      isError,
      timestamp: Date.now(),
    });
  }

  /**
   * 跑一次推理。返回本轮终结状态（TurnEnd）。
   *
   * 关键：按 `stopReason` 精确区分停止原因，**不伪造正常停止消息兜底**。模型
   * 失败（stopReason="error"）绝不映射成 answered；只有 stop/length 才是正常回答。
   *
   * 注意：Node 侧必须**持续读输入**，不能在推理期间阻塞事件循环去 await 单个
   * 工具回执——那个等待由 main.ts 的消息循环负责，通过 call_id → Promise 完成。
   *
   * 模型请求准入/结算：每次真正 `stream()` 前调用 `handlers.onModelRequest`
   * （返回 false 表示宿主拒绝，本轮不请求模型）；stream 结束后调用
   * `handlers.onModelUsage` 上报用量与结果，宿主结算确认后才允许下一轮。
   */
  async run(
    signal: AbortSignal,
    handlers: {
      onTextDelta: (delta: string) => void;
      onToolCall: (toolCall: import('@mariozechner/pi-ai').ToolCall) => void;
      onModelRequest: (requestId: string) => Promise<boolean>;
      onModelUsage: (
        requestId: string,
        usage: {
          input_tokens: number;
          output_tokens: number;
          cache_read_tokens: number;
          cache_write_tokens: number;
          total_tokens: number;
        },
        outcome: 'succeeded' | 'failed' | 'cancelled',
      ) => Promise<void>;
    },
  ): Promise<TurnEnd> {
    let answer = '';
    let sawToolCall = false;
    let finalMessage: AssistantMessage | null = null;
    let streamError: string | null = null;

    const requestId = randomUUID();

    // 准入：宿主检查控制/预算并预留费用。deny 则本轮不请求模型。
    const allowed = await handlers.onModelRequest(requestId);
    if (!allowed) {
      return {
        kind: 'error',
        error: '模型请求被宿主拒绝',
        message: {
          role: 'assistant',
          content: [{ type: 'text', text: answer }],
          api: this.model.api,
          provider: this.model.provider,
          model: this.model.id,
          usage: {
            input: 0,
            output: 0,
            cacheRead: 0,
            cacheWrite: 0,
            totalTokens: 0,
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
          },
          stopReason: 'error',
          errorMessage: '模型请求被宿主拒绝',
          timestamp: Date.now(),
        },
      };
    }

    const s = stream(this.model, this.context, {
      signal,
      apiKey: this.apiKey,
      timeoutMs: this.modelTimeoutMs,
    });

    for await (const event of s) {
      switch (event.type) {
        case 'text_delta':
          answer += event.delta;
          handlers.onTextDelta(event.delta);
          break;
        case 'toolcall_end':
          sawToolCall = true;
          handlers.onToolCall(event.toolCall);
          break;
        case 'done':
          finalMessage = event.message;
          break;
        case 'error':
          // 模型失败/中止：记录错误消息，**不**当正常结果
          finalMessage = event.error;
          streamError = event.error.errorMessage ?? `模型运行失败（${event.reason}）`;
          break;
        default:
          break;
      }
    }

    // stream 可能在未显式发出 done/error 时结束：用 result() 补最终消息。
    // 若仍拿不到最终消息，视为失败，不伪造 stop 消息兜底。
    if (!finalMessage) {
      try {
        finalMessage = await s.result();
      } catch {
        finalMessage = null;
      }
    }

    // 结算：无论成功/失败/取消都要上报，缺 usage 时由宿主保守估算，不填零。
    const outcome: 'succeeded' | 'failed' | 'cancelled' =
      streamError || (finalMessage?.stopReason === 'error')
        ? 'failed'
        : (finalMessage?.stopReason === 'aborted' ? 'cancelled' : 'succeeded');
    const u = finalMessage?.usage;
    await handlers.onModelUsage(requestId, {
      input_tokens: u?.input ?? 0,
      output_tokens: u?.output ?? 0,
      cache_read_tokens: u?.cacheRead ?? 0,
      cache_write_tokens: u?.cacheWrite ?? 0,
      total_tokens: u?.totalTokens ?? 0,
    }, outcome);

    if (!finalMessage) {
      // 无有效最终消息：明确失败，不做伪造
      return {
        kind: 'error',
        error: '模型未返回有效最终消息',
        message: {
          role: 'assistant',
          content: [{ type: 'text', text: answer }],
          api: this.model.api,
          provider: this.model.provider,
          model: this.model.id,
          usage: {
            input: 0,
            output: 0,
            cacheRead: 0,
            cacheWrite: 0,
            totalTokens: 0,
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
          },
          stopReason: 'error',
          errorMessage: '模型未返回有效最终消息',
          timestamp: Date.now(),
        },
      };
    }

    // 记录这条 assistant 消息，供后续继续（工具回执后再跑）
    this.context.messages.push(finalMessage);

    // 按 stopReason 判定：error/aborted 优先于 toolUse/stop；未知停止原因按 error。
    const reason = finalMessage.stopReason;
    const kind = classifyTurnEnd(reason, sawToolCall, streamError);
    if (kind === 'error') {
      return { kind: 'error', error: streamError ?? (reason === 'error' ? '模型运行失败' : `未知的停止原因：${String(reason)}`), message: finalMessage };
    }
    if (kind === 'aborted') {
      return { kind: 'aborted', message: finalMessage };
    }
    if (kind === 'toolUse') {
      return { kind: 'toolUse', message: finalMessage };
    }
    return { kind: 'answered', answer: answer || extractText(finalMessage), message: finalMessage };
  }

  /** 销毁：第三阶段是内存 session，无外部资源需释放。 */
  dispose(): void {
    this.context = { systemPrompt: undefined, messages: [] };
  }
}
