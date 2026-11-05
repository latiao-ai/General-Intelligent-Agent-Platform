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

export interface SessionRunResult {
  /** 最终答复文本（取 assistant 消息里的 text 块拼接） */
  answer: string;
  /** 是否因为工具调用而停止（还需要回执后继续） */
  toolUse: boolean;
  /** 最终 assistant 消息（含 usage） */
  message: AssistantMessage;
}

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
   * 跑一次推理。返回本轮结果。
   *
   * 注意：Node 侧必须**持续读输入**，不能在推理期间阻塞事件循环去 await 单个
   * 工具回执——那个等待由 main.ts 的消息循环负责，通过 call_id → Promise 完成。
   * 这里只负责发起一次 stream 并消费其事件，工具调用通过 onToolCall 回调交出去，
   * 由上层决定何时（回执到达后）再次调用 run 继续。
   */
  async run(
    signal: AbortSignal,
    handlers: {
      onTextDelta: (delta: string) => void;
      onToolCall: (toolCall: import('@mariozechner/pi-ai').ToolCall) => void;
    },
  ): Promise<SessionRunResult> {
    let answer = '';
    let toolUse = false;
    let finalMessage: AssistantMessage | null = null;

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
          toolUse = true;
          handlers.onToolCall(event.toolCall);
          break;
        case 'done':
          finalMessage = event.message;
          break;
        case 'error':
          finalMessage = event.error;
          break;
        default:
          break;
      }
    }

    // stream 可能在没有 done/error 事件（如直接返回）时结束，用 result() 兜底
    if (!finalMessage) {
      try {
        finalMessage = await s.result();
      } catch {
        finalMessage = null;
      }
    }

    const msg: AssistantMessage =
      finalMessage ??
      ({
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
        stopReason: 'stop',
        timestamp: Date.now(),
      } as AssistantMessage);

    // 记录这条 assistant 消息，供后续继续（工具回执后再跑）
    this.context.messages.push(msg);

    return {
      answer: answer || extractText(msg),
      toolUse,
      message: msg,
    };
  }

  /** 销毁：第三阶段是内存 session，无外部资源需释放。 */
  dispose(): void {
    this.context = { systemPrompt: undefined, messages: [] };
  }
}
