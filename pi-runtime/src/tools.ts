/**
 * 工具定义与工具调用→宿主请求的转换。
 *
 * 第五阶段：工具目录**由宿主经 start 消息传入完整 JSON Schema**（来自能力包
 * `Tool.args`，含 type/properties/required/嵌套对象/数组），Node 据此注册到
 * Pi session。Node 不再写死 TEST_TOOLS，也**不回退**到 echo/add：
 * `tools=[]` 表示「本轮没有任何工具」。
 *
 * 支持的 schema 范围（刻意收窄，超出则**提前拒绝**，绝不静默丢约束）：
 *   - 顶层必须是 `type: "object"`；
 *   - 字段类型支持 string / number / integer / boolean / array / object；
 *   - array 的元素用 `items`；object 用 `properties` + `required`；
 *   - 不支持 anyOf/oneOf/allOf/$ref/patternProperties 等高级关键字，遇到即报错。
 *
 * 工具执行**交回 Python**：Node 侧发 tool_call，用 call_id → Promise 等待回执。
 * 同一轮内允许 Pi 一次产生多个 tool_call，但**逐个串行回执**（一次只等一个）。
 */
import { Type, type TSchema, type Tool, type ToolCall } from '@mariozechner/pi-ai';

import type { HostToolDecl, ToolCallMessage } from './protocol.js';

/** 把一个宿主 JSON Schema 节点转成 TypeBox schema。
 *
 * 只支持「可被 Pi SDK 工具参数表达」的约束；遇到不支持的关键字抛错，
 * 让 start 明确失败，而不是静默丢掉约束。
 */
export function schemaFromJson(node: unknown): TSchema {
  if (node === null || typeof node !== 'object' || Array.isArray(node)) {
    throw new Error('工具参数 schema 节点必须是对象');
  }
  const schema = node as Record<string, unknown>;
  for (const kw of ['anyOf', 'oneOf', 'allOf', '$ref', 'patternProperties']) {
    if (kw in schema) {
      throw new Error(`不支持的 JSON Schema 关键字：${kw}`);
    }
  }
  const kind = schema.type;
  if (kind === 'object') {
    const properties = schema.properties ?? {};
    if (properties === null || typeof properties !== 'object' || Array.isArray(properties)) {
      throw new Error('object 参数的 properties 必须是对象');
    }
    const built: Record<string, TSchema> = {};
    for (const [key, sub] of Object.entries(properties as Record<string, unknown>)) {
      built[key] = schemaFromJson(sub);
    }
    const required = schema.required ?? [];
    if (!Array.isArray(required)) {
      throw new Error('object 参数的 required 必须是数组');
    }
    return Type.Object(built, {
      additionalProperties: false,
      required: required as string[],
    });
  }
  if (kind === 'array') {
    const items = schema.items ?? {};
    return Type.Array(schemaFromJson(items));
  }
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
      throw new Error(`不支持的参数类型：${String(kind)}`);
  }
}

/**
 * 把宿主传入的工具声明转成 Pi SDK 的 Tool[]。
 *
 * 只在参数为「object + 受支持字段」时支持；其他形状直接抛错，让 start 明确
 * 失败，而非静默丢失约束。空数组返回空数组（本轮无工具）。
 */
export function buildToolsFromHost(decls: HostToolDecl[]): Tool[] {
  if (!Array.isArray(decls)) {
    throw new Error('工具目录必须是数组');
  }
  return decls.map((decl) => {
    if (!decl || typeof decl.name !== 'string' || !decl.name) {
      throw new Error('工具缺少合法 name');
    }
    const params = decl.parameters ?? {};
    if (typeof params !== 'object' || Array.isArray(params) || params === null) {
      throw new Error(`工具 ${decl.name} 的参数 schema 必须是对象`);
    }
    return {
      name: decl.name,
      description: typeof decl.description === 'string' ? decl.description : '',
      parameters: schemaFromJson(params),
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
    arguments: (call.arguments ?? {}) as Record<string, unknown>,
  };
}
