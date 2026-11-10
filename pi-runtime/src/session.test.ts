/**
 * Node 侧纯函数单测：停止原因归类（未知停止原因按 error）与工具 schema 转换。
 *
 * 用 Node 内置 `node:test`，不引入额外测试依赖。运行：
 *   cd pi-runtime && npx tsc -p tsconfig.json && node --test dist/session.test.js
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { classifyTurnEnd, extractText, PiSession } from './session.js';
import { assembleToolsFromHost, buildToolsFromHost, schemaFromJson } from './tools.js';

test('时间更新保留自定义提示并替换旧时间，恢复消息不影响提示', () => {
  const session = new PiSession({
    systemPrompt: '自定义规则', tools: [], model: { id: 'test', baseUrl: 'http://localhost' },
  });
  session.importContext([{ role: 'user', content: '今天几号' }]);
  session.setCurrentTimePrompt('第一天');
  session.setCurrentTimePrompt('第二天');
  const context = (session as unknown as { context: { systemPrompt: string } }).context;
  assert.equal(context.systemPrompt, '自定义规则\n\n第二天');
  assert.equal(session.exportContext().length, 1);
});

// ---- 停止原因归类：未知停止原因必须按 error，绝不冒充 answered ----

test('已知 stop 归为 answered', () => {
  assert.equal(classifyTurnEnd('stop', false, null), 'answered');
  assert.equal(classifyTurnEnd('length', false, null), 'answered');
});

test('toolUse 优先于正常停止', () => {
  assert.equal(classifyTurnEnd('toolUse', false, null), 'toolUse');
  // 即使 stopReason 不是 toolUse，只要出现过 toolcall_end 也算要调用工具
  assert.equal(classifyTurnEnd('stop', true, null), 'toolUse');
});

test('error 停止原因归为 error', () => {
  assert.equal(classifyTurnEnd('error', false, null), 'error');
});

test('显式 streamError 优先于 stopReason', () => {
  assert.equal(classifyTurnEnd('stop', false, '模型运行失败'), 'error');
});

test('aborted 归为 aborted', () => {
  assert.equal(classifyTurnEnd('aborted', false, null), 'aborted');
});

test('未知停止原因归为 error，不冒充 answered', () => {
  // 未来 SDK 新增的停止语义字符串，绝不能静默当作成功
  assert.equal(classifyTurnEnd('someNewReason', false, null), 'error');
});

test('缺失停止原因归为 error', () => {
  assert.equal(classifyTurnEnd(undefined, false, null), 'error');
  assert.equal(classifyTurnEnd(null, false, null), 'error');
});

// ---- 工具 schema：完整 JSON Schema → TypeBox ----

test('schemaFromJson 支持 object + required + 嵌套', () => {
  const s = schemaFromJson({
    type: 'object',
    properties: {
      query: { type: 'string' },
      limit: { type: 'integer' },
      meta: {
        type: 'object',
        properties: { source: { type: 'string' } },
        required: ['source'],
      },
    },
    required: ['query'],
  });
  // TypeBox object schema 有 properties 与 required
  assert.ok(s);
});

test('schemaFromJson 把「类型或 null」的 anyOf 降级为可空字段', () => {
  // FastAPI/Pydantic 与 LibTV 惯用写法：Optional[str] → anyOf: [string, null]
  const s = schemaFromJson({ type: 'object', properties: {
    name: { anyOf: [{ type: 'string' }, { type: 'null' }], default: null },
  } });
  assert.ok(s);
});

test('schemaFromJson 支持真正的联合类型 anyOf（两个非 null 分支）', () => {
  const s = schemaFromJson({ anyOf: [{ type: 'string' }, { type: 'integer' }] });
  assert.ok(s);
});

test('schemaFromJson 支持 $defs + $ref 内联', () => {
  const s = schemaFromJson({
    type: 'object',
    $defs: { Clip: { type: 'object', properties: { id: { type: 'string' } } } },
    properties: { clip: { $ref: '#/$defs/Clip' } },
  });
  assert.ok(s);
});

test('schemaFromJson 支持 enum 约束', () => {
  const s = schemaFromJson({ type: 'string', enum: ['generate', 'edit', 'extend'] });
  assert.ok(s);
});

test('schemaFromJson 支持 pattern/minLength 等约束', () => {
  const s = schemaFromJson({
    type: 'object',
    properties: {
      key: { type: 'string', pattern: '^[A-Za-z0-9._:-]{8,255}$', minLength: 8 },
    },
  });
  assert.ok(s);
});

test('schemaFromJson 支持 additionalProperties', () => {
  const s = schemaFromJson({
    type: 'object',
    additionalProperties: true,
    properties: { a: { type: 'string' } },
  });
  assert.ok(s);
});

test('schemaFromJson 拒绝未知类型', () => {
  assert.throws(() => schemaFromJson({ type: 'tuple' }), /不支持的参数类型/);
});

test('buildToolsFromHost 空数组表示无工具，不回退 echo/add', () => {
  const tools = buildToolsFromHost([]);
  assert.deepEqual(tools, []);
});

test('buildToolsFromHost 转出带 name/description/parameters 的工具', () => {
  const tools = buildToolsFromHost([
    {
      name: 'web_search',
      description: '搜索网页',
      parameters: {
        type: 'object',
        properties: { query: { type: 'string' } },
        required: ['query'],
      },
    },
  ]);
  assert.equal(tools.length, 1);
  assert.equal(tools[0].name, 'web_search');
  assert.equal(tools[0].description, '搜索网页');
});

test('object 加 oneOf 编译后顶层仍是 object', () => {
  const tools = buildToolsFromHost([{
    name: 'inspect_media',
    description: '查看媒体',
    parameters: {
      type: 'object',
      properties: {
        fileRef: { type: 'string' },
        projectId: { type: 'string' },
      },
      oneOf: [{ required: ['fileRef'] }, { required: ['projectId'] }],
    },
  }]);
  assert.equal(tools.length, 1);
  const params = JSON.parse(JSON.stringify(tools[0].parameters));
  assert.equal(params.type, 'object');
});

test('根为 anyOf 的工具被跳过，同目录里的 object 工具仍保留', () => {
  const assembled = assembleToolsFromHost([
    {
      name: 'mcp__libtv__inspect_media',
      description: '坏根',
      parameters: { anyOf: [{ type: 'string' }, { type: 'integer' }] },
    },
    {
      name: 'get_object',
      description: '读对象',
      parameters: {
        type: 'object',
        properties: { object_id: { type: 'string' } },
        required: ['object_id'],
      },
    },
  ]);
  assert.deepEqual(assembled.tools.map((tool) => tool.name), ['get_object']);
  assert.equal(assembled.dropped.length, 1);
  assert.equal(assembled.dropped[0].name, 'mcp__libtv__inspect_media');
});

test('buildToolsFromHost 拒绝缺 name 的工具', () => {
  assert.throws(() => buildToolsFromHost([{ name: '', parameters: { type: 'object' } } as never]), /工具缺少合法 name/);
});

// ---- extractText：纯文本提取 ----

test('extractText 只取文本块', () => {
  const text = extractText({
    content: [{ type: 'text', text: '你好' }, { type: 'toolcall', id: 'x', name: 'y', arguments: {} }],
  } as never);
  assert.equal(text, '你好');
});
