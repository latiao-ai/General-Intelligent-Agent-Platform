/**
 * Pi 运行时桥接子进程入口。
 *
 * 职责：逐行 JSON 协议读写（stdout 只传协议、stderr 写日志）、消息分发、
 * session 生命周期、串行工具回执、取消与进程退出。
 *
 * 并发模型：
 *   - stdin 用 readline 逐行读，持续不断（绝不在推理期间阻塞读取）；
 *   - 推理跑在独立异步任务里；工具调用通过 call_id → Promise 等待回执，
 *     tool_result 消息到达时 resolve；
 *   - cancel 通过 AbortController 中止当前推理，并结束所有待响应的工具 Promise；
 *   - 检测到 stdin EOF（Python 异常退出）后清理并退出。
 *
 * 终止语义（第四阶段）：
 *   - `finished` 每条执行只发一次；`error` 是终结性诊断，发出后不再继续发结果；
 *   - 按 session.run 返回的 TurnEnd（answered/toolUse/error/aborted）精确判定，
 *     模型错误绝不映射成 answered；
 *   - 取消后停止派发剩余工具调用，拒绝所有 pending Promise。
 */
import * as readline from 'node:readline';

import type { ToolCall } from '@mariozechner/pi-ai';

import {
  PROTOCOL_VERSION,
  type ErrorMessage,
  type EventMessage,
  type FinishedMessage,
  type InboundMessage,
  type ReadyMessage,
  type ToolCallMessage,
} from './protocol.js';
import { PiSession } from './session.js';
import { buildToolsFromHost, DEFAULT_TOOL_DECLS, toToolCallMessage } from './tools.js';

// ---- 输出（stdout 只写协议） ----

function send(msg: object): void {
  process.stdout.write(JSON.stringify(msg) + '\n');
}

function log(...args: unknown[]): void {
  process.stderr.write('[pi-runtime] ' + args.map(String).join(' ') + '\n');
}

// ---- 状态 ----

const executionId = process.env.PI_EXECUTION_ID ?? 'unknown';
let session: PiSession | null = null;
let controller: AbortController | null = null;

/** 本轮是否已经发送过终结性消息（finished / error），防止重复发送。 */
let terminalSent = false;

// 待响应的工具调用：call_id → { resolve, timer }
interface PendingTool {
  resolve: (msg: { ok: boolean; result?: unknown; error?: string }) => void;
  timer: NodeJS.Timeout;
}
const pendingTools = new Map<string, PendingTool>();

/** 工具回执超时（毫秒），由 start 消息可覆盖，默认 120s。 */
let toolTimeoutMs = 120_000;

/** 取消标记：置位后不再派发新的工具调用。 */
let cancelled = false;

function rejectAllPending(reason: string): void {
  for (const [, pending] of pendingTools) {
    clearTimeout(pending.timer);
    pending.resolve({ ok: false, error: reason });
  }
  pendingTools.clear();
}

// ---- 终结性消息（只发一次） ----

function sendFinished(outcome: 'answered' | 'cancelled' | 'failed', fields: Partial<FinishedMessage> = {}): void {
  if (terminalSent) {
    return;
  }
  terminalSent = true;
  const fin: FinishedMessage = {
    protocol_version: PROTOCOL_VERSION,
    execution_id: executionId,
    type: 'finished',
    outcome,
    ...fields,
  };
  send(fin);
}

function sendError(error: string): void {
  // error 是终结性诊断；若已发过 finished 则不再补发 error，避免 Python 收到
  // error 退出后 Node 又继续发结果的竞争。
  if (terminalSent) {
    log('已终结，忽略诊断错误：', error);
    return;
  }
  terminalSent = true;
  const err: ErrorMessage = {
    protocol_version: PROTOCOL_VERSION,
    execution_id: executionId,
    type: 'error',
    error,
  };
  send(err);
}

// ---- 消息处理 ----

function validate(msg: InboundMessage): void {
  if (msg.protocol_version !== PROTOCOL_VERSION) {
    throw new Error(`协议版本不匹配：期望 ${PROTOCOL_VERSION}，收到 ${msg.protocol_version}`);
  }
  if (msg.execution_id !== executionId) {
    throw new Error(`execution_id 不匹配：期望 ${executionId}，收到 ${msg.execution_id}`);
  }
}

function handleToolResult(msg: Extract<InboundMessage, { type: 'tool_result' }>): void {
  const pending = pendingTools.get(msg.call_id);
  if (!pending) {
    // 未知结果 ID：明确拒绝，不静默忽略
    sendError(`未知的 tool_result call_id: ${msg.call_id}`);
    return;
  }
  pendingTools.delete(msg.call_id);
  clearTimeout(pending.timer);
  pending.resolve({ ok: msg.ok, result: msg.result, error: msg.error });
}

function handleCancel(): void {
  // 停止接纳新工具调用：中止推理，拒绝所有待响应工具 Promise
  log('收到 cancel，中止推理');
  cancelled = true;
  controller?.abort();
  rejectAllPending('已取消');
  sendFinished('cancelled');
}

function handleShutdown(): void {
  log('收到 shutdown，退出');
  controller?.abort();
  rejectAllPending('进程关闭');
  process.exit(0);
}

/** 发送一次工具调用请求，等待 Python 回执（call_id → Promise）。 */
function dispatchToolCall(call: ToolCall): Promise<{ ok: boolean; result?: unknown; error?: string }> {
  const msg: ToolCallMessage = toToolCallMessage(executionId, call);
  send(msg);
  return new Promise<{ ok: boolean; result?: unknown; error?: string }>((resolve) => {
    const timer = setTimeout(() => {
      const pending = pendingTools.get(call.id);
      if (pending) {
        pendingTools.delete(call.id);
        resolve({ ok: false, error: `工具回执超时：${call.name}` });
      }
    }, toolTimeoutMs);
    pendingTools.set(call.id, { resolve, timer });
  });
}

/** 运行一轮推理，串行处理工具调用。 */
async function runTurn(input: string, systemPrompt?: string, history?: Array<{ role: 'user' | 'assistant'; content: string }>): Promise<void> {
  if (!session) {
    throw new Error('session 未初始化');
  }
  if (history && history.length) {
    session.pushHistory(history);
  }
  session.pushUser(input);

  controller = new AbortController();

  for (;;) {
    if (cancelled) {
      return; // 已取消，不再推进
    }

    const toolCalls: ToolCall[] = [];
    const end = await session.run(controller.signal, {
      onTextDelta: (delta) => {
        const ev: EventMessage = {
          protocol_version: PROTOCOL_VERSION,
          execution_id: executionId,
          type: 'event',
          kind: 'text_delta',
          delta,
        };
        send(ev);
      },
      onToolCall: (call) => {
        toolCalls.push(call);
      },
    });

    switch (end.kind) {
      case 'answered':
        sendFinished('answered', { answer: end.answer });
        return;
      case 'error':
        sendError(end.error);
        return;
      case 'aborted':
        // 模型被中止（通常是取消）：若已由 cancel 路径发过 finished 则忽略
        if (!terminalSent) {
          sendFinished('cancelled');
        }
        return;
      case 'toolUse':
        break; // 继续串行回执工具
    }

    // 逐个串行回执；若本轮有多个工具调用，取消后不再派发剩余
    for (const call of toolCalls) {
      if (cancelled) {
        return;
      }
      const reply = await dispatchToolCall(call);
      if (cancelled) {
        // 等待回执期间被取消：不再 push 剩余结果
        return;
      }
      session.pushToolResult(call.id, call.name, reply.result, reply.ok ? false : true);
      if (!reply.ok) {
        log(`工具 ${call.name} 回执为失败：`, reply.error ?? '未知错误');
      }
    }
  }
}

async function handleStart(msg: Extract<InboundMessage, { type: 'start' }>): Promise<void> {
  if (session) {
    sendError('session 已存在，拒绝重复 start');
    return;
  }
  if (terminalSent) {
    sendError('本轮已终结，拒绝 start');
    return;
  }
  if (msg.model_timeout_ms) {
    toolTimeoutMs = Math.max(1000, msg.model_timeout_ms);
  }

  // 宿主工具目录真正生效：start 传入的工具声明转成 Pi 工具；失败则明确报错
  let tools;
  try {
    const decls = msg.tools && msg.tools.length ? msg.tools : DEFAULT_TOOL_DECLS;
    tools = buildToolsFromHost(decls);
  } catch (e) {
    sendError(e instanceof Error ? e.message : String(e));
    return;
  }

  session = new PiSession({
    systemPrompt: msg.system_prompt,
    tools,
    model: {
      id: msg.model.id,
      baseUrl: msg.model.base_url,
      provider: msg.model.provider,
      api: msg.model.api,
    },
    apiKey: msg.api_key,
    modelTimeoutMs: msg.model_timeout_ms,
  });

  try {
    await runTurn(msg.user_input, msg.system_prompt, msg.history);
  } catch (e) {
    if (controller?.signal.aborted || cancelled) {
      // 取消/关闭：只发一次 cancelled
      if (!terminalSent) {
        sendFinished('cancelled');
      }
    } else {
      const err = e instanceof Error ? e.message : String(e);
      sendError(err);
    }
  }
}

// ---- 主循环：持续读 stdin ----

const rl = readline.createInterface({
  input: process.stdin,
  crlfDelay: Infinity,
});

rl.on('line', (line) => {
  if (!line.trim()) {
    return;
  }
  let msg: InboundMessage;
  try {
    msg = JSON.parse(line) as InboundMessage;
  } catch {
    sendError('无法解析输入 JSON 行');
    return;
  }

  try {
    validate(msg);
  } catch (e) {
    sendError(e instanceof Error ? e.message : String(e));
    return;
  }

  switch (msg.type) {
    case 'start':
      // 异步处理 start，不阻塞 readline 继续读后续 tool_result / cancel
      void handleStart(msg).catch((e) => {
        log('handleStart 异常：', e);
        if (!terminalSent) {
          sendError(e instanceof Error ? e.message : String(e));
        }
      });
      break;
    case 'tool_result':
      handleToolResult(msg);
      break;
    case 'cancel':
      handleCancel();
      break;
    case 'shutdown':
      handleShutdown();
      break;
    default:
      sendError(`未知消息类型：${(msg as { type: string }).type}`);
      break;
  }
});

rl.on('close', () => {
  // stdin EOF：Python 异常退出，清理并退出
  log('stdin EOF，清理退出');
  controller?.abort();
  rejectAllPending('宿主已退出');
  session?.dispose();
  process.exit(0);
});

// 启动完成，声明协议版本
const ready: ReadyMessage = {
  protocol_version: PROTOCOL_VERSION,
  execution_id: executionId,
  type: 'ready',
};
send(ready);
log('ready，protocol_version=', PROTOCOL_VERSION, 'execution_id=', executionId);

// 防抖：异常未捕获时退出，避免留下僵尸进程
process.on('uncaughtException', (e) => {
  log('uncaughtException：', e);
  rejectAllPending('进程异常');
  process.exit(1);
});
process.on('unhandledRejection', (e) => {
  log('unhandledRejection：', e);
});
