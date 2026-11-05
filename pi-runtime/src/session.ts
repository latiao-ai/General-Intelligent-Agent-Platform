/**
 * Pi session 的显式创建、运行与销毁。
 *
 * 第三阶段使用**内存 session**：一个 Context 对象，一次运行独占。不加载持久
 * 会话存储、不加载用户目录扩展、MCP 或文件操作能力。
 *
 * 模型端点来自宿主通过 start 消息传入（含凭据），Node 侧据此构造一个
 * OpenAI 兼容的自定义 Model。凭据只经初始化通道传递，绝不写命令行参数或日志。
 */
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
   */
  async run(
    signal: AbortSignal,
    handlers: {
      onTextDelta: (delta: string) => void;
      onToolCall: (toolCall: import('@mariozechner/pi-ai').ToolCall) => void;
    },
  ): Promise<TurnEnd> {
    let answer = '';
    let sawToolCall = false;
    let finalMessage: AssistantMessage | null = null;
    let streamError: string | null = null;

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

    // 按 stopReason 判定：error/aborted 优先于 toolUse/stop
    const reason = finalMessage.stopReason;
    if (streamError || reason === 'error') {
      return { kind: 'error', error: streamError ?? '模型运行失败', message: finalMessage };
    }
    if (reason === 'aborted') {
      return { kind: 'aborted', message: finalMessage };
    }
    if (reason === 'toolUse' || sawToolCall) {
      return { kind: 'toolUse', message: finalMessage };
    }
    // stop / length（或缺失）都视为正常回答
    return { kind: 'answered', answer: answer || extractText(finalMessage), message: finalMessage };
  }

  /** 销毁：第三阶段是内存 session，无外部资源需释放。 */
  dispose(): void {
    this.context = { systemPrompt: undefined, messages: [] };
  }
}
