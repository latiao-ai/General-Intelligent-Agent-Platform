/**
 * 工具定义与工具调用→宿主请求的转换。
 *
 * 第四阶段：工具目录**由宿主经 start 消息传入**（名称、描述、完整参数 schema），
 * Node 据此注册到 Pi session。Node 不再写死 TEST_TOOLS。
 *
 * 支持的 schema 范围（刻意收窄，超出则**提前拒绝**，绝不静默丢约束）：
 *   - 参数必须是「对象」，其字段类型仅支持 string / number / boolean / integer；
 *   - 不支持的字段类型（数组、嵌套对象、union 等）在 start 时明确报错。
 *
 * 工具执行**交回 Python**：Node 侧发 tool_call，用 call_id → Promise 等待回执。
 * 同一轮内允许 Pi 一次产生多个 tool_call，但**逐个串行回执**（一次只等一个）。
 */
import { Type, type Tool, type ToolCall } from '@mariozechner/pi-ai';

import type { ToolCallMessage } from './protocol.js';

/** 宿主传入的工具声明（start 消息里的 tools 字段）。 */
export interface HostToolDecl {
  name: string;
  description: string;
  /** 参数 schema：字段名 → 类型字符串（string/number/boolean/integer） */
  parameters: Record<string, string>;
}

/** 第三阶段的默认测试工具目录（echo + add），宿主未显式传时兜底。 */
export const DEFAULT_TOOL_DECLS: HostToolDecl[] = [
  {
    name: 'echo',
    description: '回显传入的字符串，用于验证工具调用往返。',
    parameters: { text: 'string' },
  },
  {
    name: 'add',
    description: '计算两个整数之和。',
    parameters: { a: 'integer', b: 'integer' },
  },
];

/** 把一个宿主字段类型字符串转成 TypeBox schema 构造器。 */
function typeFromString(kind: string): import('typebox').TSchema {
  switch (kind) {
    case 'string':
      return Type.String();
    case 'number':
      return Type.Number();
    case 'integer':
      return Type.Integer();
    case 'boolean':
      return Type.Boolean();
    default:
      throw new Error(`不支持的参数类型：${kind}`);
  }
}

/**
 * 把宿主传入的工具声明转成 Pi SDK 的 Tool[]。
 *
 * 只在参数为「对象 + 基础标量字段」时支持；其他形状（非对象参数、数组字段、
 * 嵌套对象、union 等）直接抛错，让 start 明确失败，而非静默丢失约束。
 */
export function buildToolsFromHost(decls: HostToolDecl[]): Tool[] {
  return decls.map((decl) => {
    if (!decl || typeof decl.name !== 'string' || !decl.name) {
      throw new Error('工具缺少合法 name');
    }
    const params = decl.parameters ?? {};
    if (typeof params !== 'object' || Array.isArray(params) || params === null) {
      throw new Error(`工具 ${decl.name} 的参数 schema 必须是对象`);
    }
    const properties: Record<string, import('typebox').TSchema> = {};
    for (const [key, kind] of Object.entries(params)) {
      properties[key] = typeFromString(kind);
    }
    return {
      name: decl.name,
      description: typeof decl.description === 'string' ? decl.description : '',
      parameters: Type.Object(properties),
    };
  });
}

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
