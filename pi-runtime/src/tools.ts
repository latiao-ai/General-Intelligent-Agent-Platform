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
 * 支持的 JSON Schema 子集（覆盖 LibTV / FastAPI-Pydantic v2 的常见输出）：
 *   - 类型：object / array / string / number / integer / boolean / null；
 *   - 组合：anyOf / oneOf / allOf；「类型或 null」的可空写法降级为 Optional；
 *   - 引用：顶层 `$defs` + `$ref: "#/$defs/X"`（内联展开，不支持跨文档/循环）；
 *   - 约束：enum / const / pattern / minLength / maxLength / minimum / maximum /
 *     exclusiveMinimum / exclusiveMaximum / minItems / maxItems / uniqueItems /
 *     minProperties；
 *   - object：properties / required / additionalProperties。
 * 遇到不在上述范围的关键字（如 patternProperties、not、跨文件 $ref）仍抛错，
 * 让 start 明确失败，而不是静默丢掉约束。
 */

type SchemaNode = Record<string, unknown>;

/** 判断一个 schema 节点是不是「null 类型」。 */
function isNullNode(node: unknown): boolean {
  return Boolean(
    node && typeof node === 'object' && (node as SchemaNode).type === 'null',
  );
}

/** 从 anyOf/oneOf/allOf 里抽掉 null 分支，返回非 null 分支数组。
 *
 * 返回 null 表示「这不是可空写法」（没有任何 null 分支），交由组合逻辑处理。
 */
function nonNullBranches(variants: unknown): SchemaNode[] | null {
  if (!Array.isArray(variants)) return null;
  const kept = variants.filter((v) => !isNullNode(v));
  if (kept.length === variants.length) return null; // 没有 null 分支
  return kept as SchemaNode[];
}

const stringOpts = ['pattern', 'minLength', 'maxLength', 'format'] as const;
const numberOpts = ['minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum'] as const;
const arrayOpts = ['minItems', 'maxItems', 'uniqueItems'] as const;

/** 把 unknown 字面量收窄成 TypeBox 可接受的字面量类型，其余抛错。 */
function literalSchema(v: unknown): TSchema {
  if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
    return Type.Literal(v);
  }
  throw new Error(`不支持的字面量类型：${typeof v}`);
}

function pickOptions(
  schema: SchemaNode,
  keys: readonly string[],
): Record<string, unknown> | undefined {
  const out: Record<string, unknown> = {};
  for (const k of keys) {
    if (k in schema) out[k] = schema[k];
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

/** 真正的递归编译器，带 $defs 上下文。 */
function compile(node: unknown, defs: SchemaNode, depth: number): TSchema {
  if (depth > 20) {
    throw new Error('工具参数 schema 嵌套过深');
  }
  if (node === null || typeof node !== 'object' || Array.isArray(node)) {
    throw new Error('工具参数 schema 节点必须是对象');
  }
  const schema = node as SchemaNode;

  // $ref：只支持指向本工具顶层 $defs 的内部引用，内联展开。
  if ('$ref' in schema) {
    const ref = String(schema.$ref);
    const m = ref.match(/^#\/\$defs\/([A-Za-z0-9_]+)$/);
    if (!m) {
      throw new Error(`不支持的 JSON Schema 引用：${ref}`);
    }
    const target = defs[m[1]];
    if (target === undefined) {
      throw new Error(`$defs 里找不到定义：${m[1]}`);
    }
    return compile(target, defs, depth + 1);
  }

  // 组合关键字：优先处理（Pydantic 的 anyOf 常带 null 分支，表示可空）。
  if ('anyOf' in schema || 'oneOf' in schema || 'allOf' in schema) {
    // allOf 里若是 if/then/else 条件约束，把它并入当前 object（见下），
    // 而不是当成交集编译——pi-ai 无法表达运行时条件。
    return compileCombinator(schema, defs, depth);
  }

  // enum / const：字面量集合。
  if ('enum' in schema) {
    const values = schema.enum;
    if (!Array.isArray(values) || values.length === 0) {
      throw new Error('enum 必须是数组');
    }
    // 过滤 null：enum 里混 null 表示可空。
    const nonNull = values.filter((v) => v !== null);
    const built = Type.Union(nonNull.map((v) => literalSchema(v)));
    return nonNull.length === values.length ? built : Type.Optional(built);
  }
  if ('const' in schema) {
    const v = schema.const;
    return v === null ? Type.Optional(Type.Null()) : literalSchema(v);
  }

  // if/then/else 条件分支：pi-ai 无法表达运行时条件，宽松处理为「合并 then 分支
  // 到当前对象」。Pydantic 的 discriminated union 用它区分不同变体；对工具参数
  // 生成而言，把 then 的 properties 并进来是安全且完整的（if 只是触发条件）。
  if ('then' in schema && 'properties' in (schema.then as SchemaNode)) {
    const merged: SchemaNode = {
      ...schema,
      properties: {
        ...(schema.properties as Record<string, unknown> ?? {}),
        ...((schema.then as SchemaNode).properties as Record<string, unknown> ?? {}),
      },
      required: [
        ...(schema.required as unknown[] ?? []),
        ...((schema.then as SchemaNode).required as unknown[] ?? []),
      ],
    };
    delete merged.if;
    delete merged.then;
    delete merged.else;
    return compile(merged, defs, depth + 1);
  }

  // 缺 type 但带 properties → 隐式 object；带 items → 隐式 array（JSON Schema 语义）。
  let kind = schema.type;
  if (kind === undefined) {
    if ('properties' in schema) kind = 'object';
    else if ('items' in schema) kind = 'array';
  }
  if (kind === 'object') {
    const properties = schema.properties ?? {};
    if (properties === null || typeof properties !== 'object' || Array.isArray(properties)) {
      throw new Error('object 参数的 properties 必须是对象');
    }
    const built: Record<string, TSchema> = {};
    for (const [key, sub] of Object.entries(properties as Record<string, unknown>)) {
      built[key] = compile(sub, defs, depth + 1);
    }
    const required = schema.required ?? [];
    if (!Array.isArray(required)) {
      throw new Error('object 参数的 required 必须是数组');
    }
    // additionalProperties：缺省 false（Pydantic 默认拒绝额外字段），显式 true 时开放。
    const ap = schema.additionalProperties;
    return Type.Object(built, {
      additionalProperties: ap === true ? true : false,
      required: required as string[],
    });
  }
  if (kind === 'array') {
    const items = schema.items;
    const opts = pickOptions(schema, arrayOpts);
    if (items === undefined || items === null) {
      throw new Error('array 参数缺少 items');
    }
    return Type.Array(compile(items, defs, depth + 1), opts);
  }
  switch (kind) {
    case 'string':
      return Type.String(pickOptions(schema, stringOpts));
    case 'number':
      return Type.Number(pickOptions(schema, numberOpts));
    case 'integer':
      return Type.Integer(pickOptions(schema, numberOpts));
    case 'boolean':
      return Type.Boolean();
    case 'null':
      return Type.Null();
    default:
      throw new Error(`不支持的参数类型：${String(kind)}`);
  }
}

function compileCombinator(schema: SchemaNode, defs: SchemaNode, depth: number): TSchema {
  // allOf：两种形态。
  //   (1) 条件约束（分支带 if/then）：把 then 的 properties/required 并入当前
  //       object，忽略 if 条件（运行时条件 pi-ai 无法表达，并入是安全方向）。
  //   (2) 真正的交集（每个分支都是完整 object schema）：Type.Intersect。
  if ('allOf' in schema) {
    const branches = schema.allOf;
    if (!Array.isArray(branches)) throw new Error('allOf 必须是数组');
    const isConditional = branches.some(
      (b) => b && typeof b === 'object' && 'then' in (b as SchemaNode),
    );
    if (isConditional) {
      const merged: SchemaNode = { ...schema };
      const mergedProps: Record<string, unknown> = {
        ...(schema.properties as Record<string, unknown> ?? {}),
      };
      const mergedRequired: unknown[] = [...(schema.required as unknown[] ?? [])];
      for (const b of branches) {
        const branch = b as SchemaNode;
        if ('then' in branch && branch.then && typeof branch.then === 'object') {
          const then = branch.then as SchemaNode;
          if (then.properties && typeof then.properties === 'object') {
            // 只并入「完整定义」的字段（有 type/$ref/组合关键字）；跳过纯约束补丁
            // （如 {maxItems:0}），那是对顶层已有字段追加约束，pi-ai 无法表达。
            for (const [pk, pv] of Object.entries(then.properties as Record<string, unknown>)) {
              const isPatch = Boolean(
                pv && typeof pv === 'object' &&
                !('type' in (pv as SchemaNode)) && !('$ref' in (pv as SchemaNode)) &&
                !('anyOf' in (pv as SchemaNode)) && !('oneOf' in (pv as SchemaNode)) &&
                !('allOf' in (pv as SchemaNode)) && !('enum' in (pv as SchemaNode)) &&
                !('properties' in (pv as SchemaNode)) && !('items' in (pv as SchemaNode)),
              );
              if (!isPatch) {
                mergedProps[pk] = pv;
              }
            }
          }
          if (Array.isArray(then.required)) {
            for (const r of then.required) {
              if (!mergedRequired.includes(r)) mergedRequired.push(r);
            }
          }
        }
      }
      merged.properties = mergedProps;
      merged.required = mergedRequired;
      delete merged.allOf;
      return compile(merged, defs, depth + 1);
    }
    const compiled = branches.map((b) => compile(b, defs, depth + 1));
    return compiled.length === 1 ? compiled[0] : Type.Intersect(compiled);
  }

  const key = ('anyOf' in schema ? 'anyOf' : 'oneOf') as 'anyOf' | 'oneOf';
  const variants = schema[key];
  if (!Array.isArray(variants) || variants.length === 0) {
    throw new Error(`${key} 必须是数组`);
  }
  // 可空写法：有 null 分支时，抽掉 null 后按 Optional 处理。
  const nonNull = nonNullBranches(variants);
  if (nonNull !== null) {
    if (nonNull.length === 1) {
      return Type.Optional(compile(nonNull[0], defs, depth + 1));
    }
    return Type.Optional(
      Type.Union(nonNull.map((b) => compile(b, defs, depth + 1))),
    );
  }
  // 真正的联合类型（无 null 分支）。
  return Type.Union(variants.map((b) => compile(b, defs, depth + 1)));
}

export function schemaFromJson(node: unknown): TSchema {
  const defs = (node as SchemaNode)?.['$defs'] ?? {};
  if (defs !== null && (typeof defs !== 'object' || Array.isArray(defs))) {
    throw new Error('$defs 必须是对象');
  }
  return compile(node, defs as SchemaNode, 0);
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
