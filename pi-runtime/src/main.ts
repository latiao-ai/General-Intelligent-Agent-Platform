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
  type CheckpointAckMessage,
  type ErrorMessage,
  type EventMessage,
  type FinishedMessage,
  type InboundMessage,
  type ModelRequestMessage,
  type ModelUsageMessage,
  type ReadyMessage,
  type StartMessage,
  type ToolCallMessage,
} from './protocol.js';
import { PiSession } from './session.js';
import { assembleToolsFromHost, toToolCallMessage } from './tools.js';

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
  resolve: (msg: {
    ok: boolean;
    result?: unknown;
    error?: string;
    accepted?: boolean;
    remote_task_id?: string;
    needs_approval?: boolean;
    approval_id?: string;
    needs_input?: boolean;
  }) => void;
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

// ---- 模型请求准入/结算握手 ----

// 待准入的模型请求：request_id → { resolve(allow), timer }
interface PendingModelRequest {
  resolve: (allow: boolean) => void;
  timer: NodeJS.Timeout;
}
const pendingModelRequests = new Map<string, PendingModelRequest>();

// 待结算确认的模型请求：request_id → { resolve(ok), timer }
interface PendingModelUsage {
  resolve: (ok: boolean) => void;
  timer: NodeJS.Timeout;
}
const pendingModelUsage = new Map<string, PendingModelUsage>();

/** 模型请求准入超时（毫秒）：宿主无应答时默认放行（兼容无治理 smoke 场景）。 */
let modelRequestTimeoutMs = 30_000;

/** 发 model_request 并等待宿主 model_admit。返回是否允许真正请求模型。 */
function requestModelAdmission(requestId: string): Promise<boolean> {
  const msg: ModelRequestMessage = {
    protocol_version: PROTOCOL_VERSION,
    execution_id: executionId,
    type: 'model_request',
    request_id: requestId,
  };
  send(msg);
  return new Promise<boolean>((resolve) => {
    const timer = setTimeout(() => {
      pendingModelRequests.delete(requestId);
      // 第七阶段：准入超时**默认拒绝**（停止请求），不再放行。
      // 无治理 smoke/测试由宿主显式回 model_admit(allow=true) 放行。
      resolve(false);
    }, modelRequestTimeoutMs);
    pendingModelRequests.set(requestId, { resolve, timer });
  });
}

/** 发 model_usage 并等待宿主 model_usage_ack（结算确认）。
 *
 * 返回布尔：true 表示宿主已确认结算；false 表示结算失败/超时。
 * 第七阶段：结算失败**不得继续下一轮**——未决费用沿用现有存储记录并对账，
 * 不静默吞掉。
 */
function reportModelUsage(
  requestId: string,
  usage: { input_tokens: number; output_tokens: number; cache_read_tokens: number; cache_write_tokens: number; total_tokens: number },
  outcome: 'succeeded' | 'failed' | 'cancelled',
): Promise<boolean> {
  const msg: ModelUsageMessage = {
    protocol_version: PROTOCOL_VERSION,
    execution_id: executionId,
    type: 'model_usage',
    request_id: requestId,
    outcome,
    usage,
  };
  send(msg);
  return new Promise<boolean>((resolve) => {
    const timer = setTimeout(() => {
      pendingModelUsage.delete(requestId);
      resolve(false); // 超时：未确认结算，视为失败（不静默吞）
    }, modelRequestTimeoutMs);
    pendingModelUsage.set(requestId, { resolve, timer });
  });
}

function handleModelAdmit(msg: Extract<InboundMessage, { type: 'model_admit' }>): void {
  const pending = pendingModelRequests.get(msg.request_id);
  if (!pending) {
    return; // 未知 request_id：忽略（可能已超时）
  }
  pendingModelRequests.delete(msg.request_id);
  clearTimeout(pending.timer);
  if (msg.allow && typeof msg.current_time_prompt === 'string') {
    session?.setCurrentTimePrompt(msg.current_time_prompt);
  }
  pending.resolve(!!msg.allow);
}

function handleModelUsageAck(msg: Extract<InboundMessage, { type: 'model_usage_ack' }>): void {
  const pending = pendingModelUsage.get(msg.request_id);
  if (!pending) {
    return;
  }
  pendingModelUsage.delete(msg.request_id);
  clearTimeout(pending.timer);
  // 第七阶段：ack 带 ok 表示结算是否成功；缺省视为成功（向后兼容旧宿主）。
  pending.resolve(msg.ok !== false);
}

function rejectAllModelRequests(reason: string): void {
  for (const [, pending] of pendingModelRequests) {
    clearTimeout(pending.timer);
    pending.resolve(false); // 取消/退出时不再放行模型请求
  }
  pendingModelRequests.clear();
  for (const [, pending] of pendingModelUsage) {
    clearTimeout(pending.timer);
    pending.resolve(false); // 取消/退出时结算未确认
  }
  pendingModelUsage.clear();
}

// ---- 检查点握手（第七阶段） ----

// 待确认的检查点：checkpoint_seq → resolve
interface PendingCheckpoint {
  resolve: (ok: boolean) => void;
  timer: NodeJS.Timeout;
}
const pendingCheckpoints = new Map<number, PendingCheckpoint>();

/** 检查点序号：单调递增，每次发检查点 +1。 */
let checkpointSeq = 0;

/** 检查点确认超时（毫秒）：宿主无应答时按失败处理（不阻塞，但恢复以库里为准）。 */
const checkpointTimeoutMs = 30_000;

/**
 * 发版本化上下文检查点，等待宿主持久化确认后才返回（允许后续派发）。
 *
 * 边界：完整模型轮次结束、工具调用候选确定。只有宿主 ack 后，工具结果才
 * 允许继续派发——否则进程在「结果已产出但没落库」之间崩溃，恢复会漏结果。
 */
function sendCheckpoint(
  messages: Array<Record<string, unknown>>,
  pendingCalls: Array<Record<string, unknown>>,
  finalAnswer?: string,
): Promise<boolean> {
  const seq = ++checkpointSeq;
  const msg: { protocol_version: number; execution_id: string; type: string; checkpoint_seq: number; messages: Array<Record<string, unknown>>; pending_calls: Array<Record<string, unknown>>; final_answer?: string } = {
    protocol_version: PROTOCOL_VERSION,
    execution_id: executionId,
    type: 'checkpoint',
    checkpoint_seq: seq,
    messages,
    pending_calls: pendingCalls,
  };
  if (finalAnswer !== undefined) {
    msg.final_answer = finalAnswer;
  }
  send(msg);
  return new Promise<boolean>((resolve) => {
    const timer = setTimeout(() => {
      pendingCheckpoints.delete(seq);
      resolve(false); // 超时：宿主未确认，恢复以库里为准
    }, checkpointTimeoutMs);
    pendingCheckpoints.set(seq, { resolve, timer });
  });
}

function handleCheckpointAck(msg: Extract<InboundMessage, { type: 'checkpoint_ack' }>): void {
  const pending = pendingCheckpoints.get(msg.checkpoint_seq);
  if (!pending) {
    return;
  }
  pendingCheckpoints.delete(msg.checkpoint_seq);
  clearTimeout(pending.timer);
  pending.resolve(!!msg.ok);
}

function rejectAllCheckpoints(reason: string): void {
  for (const [, pending] of pendingCheckpoints) {
    clearTimeout(pending.timer);
    pending.resolve(false);
  }
  pendingCheckpoints.clear();
}

// ---- 终结性消息（只发一次） ----

function sendFinished(outcome: 'answered' | 'cancelled' | 'failed' | 'waiting', fields: Partial<FinishedMessage> = {}): void {
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
  pending.resolve({
    ok: msg.ok,
    result: msg.result,
    error: msg.error,
    accepted: !!msg.accepted,
    remote_task_id: msg.remote_task_id,
    needs_approval: !!msg.needs_approval,
    approval_id: msg.approval_id,
    needs_input: !!msg.needs_input,
  });
}

function handleCancel(): void {
  // 停止接纳新工具调用：中止推理，拒绝所有待响应工具 Promise
  log('收到 cancel，中止推理');
  cancelled = true;
  controller?.abort();
  rejectAllPending('已取消');
  rejectAllModelRequests('已取消');
  rejectAllCheckpoints('已取消');
  sendFinished('cancelled');
}

function handleShutdown(): void {
  log('收到 shutdown，退出');
  controller?.abort();
  rejectAllPending('进程关闭');
  rejectAllModelRequests('进程关闭');
  rejectAllCheckpoints('进程关闭');
  process.exit(0);
}

/** 发送一次工具调用请求，等待 Python 回执（call_id → Promise）。 */
function dispatchToolCall(call: ToolCall): Promise<{ ok: boolean; result?: unknown; error?: string; accepted?: boolean; remote_task_id?: string; needs_approval?: boolean; approval_id?: string; needs_input?: boolean }> {
  // 执行内重复 call_id 检查：同一个 call_id 已在等待回执，说明 Pi 重复派发，
  // 明确报错而不是覆盖旧 Promise（否则旧的会永久挂起、新的丢失回执）。
  if (pendingTools.has(call.id)) {
    sendError(`重复的工具 call_id：${call.id}`);
    return Promise.resolve({ ok: false, error: `重复的工具 call_id：${call.id}` });
  }
  const msg: ToolCallMessage = toToolCallMessage(executionId, call);
  send(msg);
  return new Promise<{ ok: boolean; result?: unknown; error?: string; accepted?: boolean; remote_task_id?: string; needs_approval?: boolean; approval_id?: string; needs_input?: boolean }>((resolve) => {
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
async function runTurn(input: string, systemPrompt?: string, history?: Array<{ role: 'user' | 'assistant'; content: string }>, resume?: StartMessage['resume']): Promise<void> {
  if (!session) {
    throw new Error('session 未初始化');
  }
  if (resume && Array.isArray(resume.messages) && resume.messages.length) {
    // 恢复：用已提交的完整消息重建上下文，**不追加原始用户输入**
    session.importContext(resume.messages);
    // 第七阶段收口：检查点序号从已提交值继续，不从 0 重新开始。
    if (typeof resume.checkpoint_seq === 'number' && resume.checkpoint_seq > 0) {
      checkpointSeq = resume.checkpoint_seq;
    }
    // 第十阶段收口第 5 点：新增的补充要求作为**明确的新消息**进入恢复后 context。
    // importContext 后逐条 pushUser（原文 + 序号），让模型把「新要求」和「旧上下
    // 文」分开，而不是只合并进 user_input 一句话。
    if (Array.isArray(resume.pending_inputs) && resume.pending_inputs.length) {
      for (const p of resume.pending_inputs) {
        if (p && typeof p.message === 'string' && p.message.trim()) {
          session.pushUser(p.message);
        }
      }
    }
  } else {
    if (history && history.length) {
      session.pushHistory(history);
    }
    session.pushUser(input);
  }

  controller = new AbortController();

  // 第八阶段收口第 3/4 点：外部任务完成后，宿主把已登记结果读回并交给 Node
  // 直接注入 toolResult（不重新派发工具），再进入模型循环。必须先注入这些
  // 结果（补上上一轮「异步受理、结果未定」而缺掉的 toolResult），模型才能在
  // 下一步看到完整上下文继续推理。
  if (resume && Array.isArray(resume.deliver_results) && resume.deliver_results.length) {
    for (const d of resume.deliver_results) {
      session.pushToolResult(d.call_id, d.tool_name, d.result, !!d.is_error);
    }
    // 注入结果后落一次检查点，宿主确认再进入下一轮。
    if (!(await sendCheckpoint(session.exportContext(), []))) {
      sendError('外部结果注入检查点未被宿主确认，停止后续推理');
      return;
    }
  }

  // 第七阶段收口：恢复模式下，先重派发原待处理调用（resume_admit / resume_deliver），
  // 再进入模型循环。导入消息后**不能**一律立即请求模型——先把上一轮没处理完的
  // 工具调用补上（宿主按账本 replay 或执行），也不重复追加原始用户输入。
  if (resume && (resume.mode === 'admit' || resume.mode === 'deliver')
      && Array.isArray(resume.pending_calls) && resume.pending_calls.length) {
    for (const pending of resume.pending_calls) {
      if (cancelled) {
        return;
      }
      // 构造 ToolCall 形状，复用原 call_id / name / arguments
      const call: ToolCall = {
        type: 'toolCall',
        id: pending.call_id,
        name: pending.tool_name,
        arguments: (pending.arguments ?? {}) as Record<string, unknown>,
      };
      const reply = await dispatchToolCall(call);
      if (cancelled) {
        return;
      }
      if (reply.accepted) {
        sendFinished('waiting', { wait_reason: 'external' });
        return;
      }
      if (reply.needs_approval) {
        sendFinished('waiting', { wait_reason: 'approval' });
        return;
      }
      if (reply.needs_input) {
        sendFinished('waiting', { wait_reason: 'input' });
        return;
      }
      if (reply.ok) {
        session.pushToolResult(call.id, call.name, reply.result, false);
      } else {
        session.pushToolResult(call.id, call.name, reply.error ?? '工具执行失败', true);
      }
    }
    // 补完结果后落一次检查点，宿主确认再进入下一轮
    if (!(await sendCheckpoint(session.exportContext(), []))) {
      sendError('恢复补交检查点未被宿主确认，停止后续推理');
      return;
    }
  }

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
      onModelRequest: (requestId) => requestModelAdmission(requestId),
      onModelUsage: (requestId, usage, outcome) =>
        reportModelUsage(requestId, usage, outcome),
    });

    switch (end.kind) {
      case 'answered':
        // 最终答复候选确定：先落检查点（含最终答复候选），宿主确认后才发
        // finished。这样「最终答复已产、进程崩溃」时恢复能按 resume_finish
        // 只补收尾、不重新推理。第七阶段收口：检查点失败/超时/失租 → 停止。
        if (!(await sendCheckpoint(session.exportContext(), [], end.answer))) {
          sendError('最终答复检查点未被宿主确认，停止收尾');
          return;
        }
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

    // 工具调用候选已确定：先落检查点（含待处理调用），宿主确认后再派发。
    // 这样「结果已产出但进程崩溃」时，恢复能重新交付同一结果而不重复执行。
    // 第七阶段收口：待处理调用用唯一版本化结构（call_id / logical_call_id 占位 /
    // tool_name / arguments / seq / status），与 Python 注册表、动作回执、恢复
    // 决策共用同一映射。
    const pendingCalls = toolCalls.map((c, i) => ({
      call_id: c.id,
      logical_call_id: '',
      tool_name: c.name,
      tool_key: '',
      arguments: c.arguments ?? {},
      args_hash: '',
      seq: i,
      status: 'pending',
    }));
    // 第七阶段收口：检查点未被宿主确认（失败/超时/失租）→ 停止工具派发，
    // 不允许继续运行。
    if (!(await sendCheckpoint(session.exportContext(), pendingCalls))) {
      sendError('工具候选检查点未被宿主确认，停止派发');
      return;
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
      // 第八阶段：远端已受理、结果未定 → 结束本轮、释放进程等待唤醒。
      // 不注入 toolResult、不继续请求模型。
      if (reply.accepted) {
        sendFinished('waiting', { wait_reason: 'external' });
        return;
      }
      // 第十阶段：需要批准 → 同 accepted，结束本轮等待唤醒。不把「需要批准」
      // 当错误让模型继续尝试。
      if (reply.needs_approval) {
        sendFinished('waiting', { wait_reason: 'approval' });
        return;
      }
      if (reply.needs_input) {
        sendFinished('waiting', { wait_reason: 'input' });
        return;
      }
      // 失败回执：向模型传 reply.error（保留原始错误正文），不能继续传空的
      // reply.result —— 否则模型会拿到「成功但结果为空」，无法区分失败。
      if (reply.ok) {
        session.pushToolResult(call.id, call.name, reply.result, false);
      } else {
        session.pushToolResult(call.id, call.name, reply.error ?? '工具执行失败', true);
      }
    }

    // 工具结果已吸收进上下文：再落一次检查点，宿主确认后才进入下一轮推理。
    // 第七阶段收口：确认失败 → 停止，不进入下一轮模型请求。
    if (!(await sendCheckpoint(session.exportContext(), []))) {
      sendError('工具结果检查点未被宿主确认，停止后续推理');
      return;
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

  // 宿主工具目录真正生效：start 传入的完整 JSON Schema 工具声明转成 Pi 工具；
  // tools=[] 表示本轮无工具，**不回退** echo/add。失败则明确报错。
  let tools;
  try {
    const assembled = assembleToolsFromHost(msg.tools ?? []);
    if (assembled.dropped.length) {
      log('已跳过参数 schema 不合格的工具：',
        assembled.dropped.map((item) => item.name).join(','));
    }
    tools = assembled.tools;
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
    mockScript: msg.mock,
  });

  try {
    await runTurn(msg.user_input, msg.system_prompt, msg.history, msg.resume);
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
    case 'model_admit':
      handleModelAdmit(msg);
      break;
    case 'model_usage_ack':
      handleModelUsageAck(msg);
      break;
    case 'checkpoint_ack':
      handleCheckpointAck(msg);
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
  rejectAllModelRequests('宿主已退出');
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
