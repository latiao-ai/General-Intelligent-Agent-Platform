/**
 * 测试工具定义与工具调用→宿主请求的转换。
 *
 * 第三阶段只注册 echo 和 add 两个测试工具，不加载用户目录扩展、MCP 或文件能力。
 * 工具执行**交回 Python**：Node 侧发 tool_call，用 call_id → Promise 等待回执。
 * 同一轮内允许 Pi 一次产生多个 tool_call，但**逐个串行回执**（一次只等一个）。
 */
import { Type, type Tool, type ToolCall } from '@mariozechner/pi-ai';

import type { ToolCallMessage } from './protocol.js';

/** echo：回显字符串（覆盖工具成功路径） */
export const echoTool: Tool = {
  name: 'echo',
  description: '回显传入的字符串，用于验证工具调用往返。',
  parameters: Type.Object({
    text: Type.String({ description: '要回显的字符串' }),
  }),
};

/** add：整数加法（覆盖工具参数与"17+25=42"联调） */
export const addTool: Tool = {
  name: 'add',
  description: '计算两个整数之和。',
  parameters: Type.Object({
    a: Type.Number({ description: '第一个整数' }),
    b: Type.Number({ description: '第二个整数' }),
  }),
};

/** 第三阶段的测试工具目录（只这两个，不加载外部能力）。 */
export const TEST_TOOLS: Tool[] = [echoTool, addTool];

/** 把 Pi 的 toolcall_end 事件转成发给宿主的 tool_call 消息。 */
export function toToolCallMessage(
  executionId: string,
  call: ToolCall,
): ToolCallMessage {
  return {
    protocol_version: 1,
    execution_id: executionId,
    type: 'tool_call',
    call_id: call.id,
    tool_name: call.name,
    arguments: call.arguments ?? {},
  };
}
