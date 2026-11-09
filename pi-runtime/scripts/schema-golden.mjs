/**
 * 工具参数 schema 的**黄金对拍**：确保 dist/tools.js 的编译结果不漂移。
 *
 * 背景：`src/tools.ts` 原先用 TypeBox 的 `Type.*` 拼 JSON Schema，为了让每次
 * 启动少付约 3.7s（TypeBox 整包 import 的代价），改为等价的本地构造器。
 * 「等价」不能靠肉眼保证——模型端点对 schema 形状敏感（顶层必须 type: object，
 * 否则整条请求 400），所以这里把每条编译分支的产出固化成基线，任何改动都必须
 * 让对拍保持全绿。
 *
 * 用法：
 *   npm run build                    # 先编译
 *   node scripts/schema-golden.mjs   # 校验（不一致则非零退出）
 *   node scripts/schema-golden.mjs --update   # 有意变更时重建基线
 */
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const BASELINE = join(here, 'schema-golden.baseline.json');
const update = process.argv.includes('--update');

/** 覆盖 src/tools.ts 里每条分支，含 Pydantic / MCP 常见的畸形形状。 */
const CASES = [
  { name: 'plain-string', schema: { type: 'object', properties: { query: { type: 'string' }, limit: { type: 'integer' } }, required: ['query'], additionalProperties: false } },
  { name: 'all-scalar-kinds', schema: { type: 'object', properties: { s: { type: 'string' }, n: { type: 'number' }, i: { type: 'integer' }, b: { type: 'boolean' }, z: { type: 'null' } }, required: ['s', 'n', 'i', 'b', 'z'] } },
  { name: 'string-constraints', schema: { type: 'object', properties: { q: { type: 'string', pattern: '^[a-z]+$', minLength: 2, maxLength: 20, format: 'uri' }, num: { type: 'number', minimum: 0, maximum: 10, exclusiveMinimum: -1, exclusiveMaximum: 11 } }, required: ['q', 'num'] } },
  { name: 'array-constraints', schema: { type: 'object', properties: { tags: { type: 'array', items: { type: 'string' }, minItems: 1, maxItems: 5, uniqueItems: true } }, required: ['tags'] } },
  { name: 'nested-object', schema: { type: 'object', properties: { outer: { type: 'object', properties: { inner: { type: 'string' } }, required: ['inner'], additionalProperties: true } }, required: ['outer'], additionalProperties: false } },
  { name: 'enum-without-null', schema: { type: 'object', properties: { kind: { enum: ['a', 'b', 3, true] } }, required: ['kind'] } },
  { name: 'enum-with-null', schema: { type: 'object', properties: { kind: { enum: ['a', 'b', null] } }, required: ['kind'] } },
  { name: 'const-values', schema: { type: 'object', properties: { k: { const: 'fixed' }, nz: { const: null } }, required: ['k', 'nz'] } },
  { name: 'anyof-nullable', schema: { type: 'object', properties: { maybe: { anyOf: [{ type: 'string' }, { type: 'null' }] } }, required: ['maybe'] } },
  { name: 'anyof-union-no-null', schema: { type: 'object', properties: { v: { anyOf: [{ type: 'string' }, { type: 'number' }] } }, required: ['v'] } },
  { name: 'oneof-union', schema: { type: 'object', properties: { v: { oneOf: [{ type: 'boolean' }, { type: 'string' }] } }, required: ['v'] } },
  { name: 'object-with-oneof-variants', schema: { type: 'object', properties: { fileRef: { type: 'string' }, projectId: { type: 'null' }, assetId: { type: 'null' } }, oneOf: [{ required: ['fileRef'] }, { anyOf: [{ required: ['projectId'] }, { required: ['assetId'] }] }], additionalProperties: false } },
  { name: 'defs-ref', schema: { type: 'object', properties: { item: { $ref: '#/$defs/Item' } }, required: ['item'], $defs: { Item: { type: 'object', properties: { id: { type: 'string' } }, required: ['id'] } } } },
  { name: 'implicit-object', schema: { type: 'object', properties: { wrap: { properties: { a: { type: 'string' } }, required: ['a'] } }, required: ['wrap'] } },
  { name: 'implicit-array', schema: { type: 'object', properties: { list: { items: { type: 'integer' } } }, required: ['list'] } },
  { name: 'then-branch-merge', schema: { type: 'object', properties: { mode: { type: 'string' } }, required: ['mode'], then: { properties: { extra: { type: 'string' } }, required: ['extra'] } } },
  { name: 'allof-intersect', schema: { type: 'object', properties: { v: { allOf: [{ type: 'string' }, { type: 'string' }] } }, required: ['v'] } },
  { name: 'deep-nesting', schema: { type: 'object', properties: { l1: { type: 'object', properties: { l2: { type: 'array', items: { type: 'object', properties: { l3: { type: 'string' } }, required: ['l3'] } } }, required: ['l2'] } }, required: ['l1'] } },
  { name: 'null-required-field', schema: { type: 'object', properties: { x: { type: 'null' } }, required: ['x'], additionalProperties: false } },
  { name: 'additional-props-default', schema: { type: 'object', properties: { a: { type: 'string' } }, required: ['a'] } },
];

/** 键序无关的规范化：schema 的键序在语义上无意义，不该造成假失败。 */
function norm(x) {
  if (Array.isArray(x)) return x.map(norm);
  if (x && typeof x === 'object') {
    const out = {};
    for (const k of Object.keys(x).sort()) out[k] = norm(x[k]);
    return out;
  }
  return x;
}

const { schemaFromJson } = await import(new URL('../dist/tools.js', import.meta.url));

const actual = { result: {}, failures: {} };
for (const { name, schema } of CASES) {
  try {
    actual.result[name] = norm(JSON.parse(JSON.stringify(schemaFromJson(schema))));
  } catch (err) {
    actual.failures[name] = String(err?.message ?? err);
  }
}

if (update || !existsSync(BASELINE)) {
  writeFileSync(BASELINE, JSON.stringify(actual, null, 1) + '\n');
  console.log(`${update ? '已重建' : '首次生成'}基线：${BASELINE}`);
  console.log(`  用例 ${Object.keys(actual.result).length} 个，抛错 ${Object.keys(actual.failures).length} 个`);
  process.exit(0);
}

const baseline = JSON.parse(readFileSync(BASELINE, 'utf8'));
const names = new Set([...Object.keys(baseline.result), ...Object.keys(actual.result)]);
const diffs = [];
for (const name of names) {
  const a = JSON.stringify(baseline.result[name]);
  const b = JSON.stringify(actual.result[name]);
  if (a !== b) diffs.push(name);
}
const failDiffs = names.size
  ? Object.keys({ ...baseline.failures, ...actual.failures })
      .filter((k) => baseline.failures[k] !== actual.failures[k])
  : [];

if (diffs.length || failDiffs.length) {
  console.error('schema 对拍失败：');
  for (const name of diffs) {
    console.error(`  ${name}`);
    console.error(`    基线: ${JSON.stringify(baseline.result[name])?.slice(0, 300)}`);
    console.error(`    当前: ${JSON.stringify(actual.result[name])?.slice(0, 300)}`);
  }
  for (const name of failDiffs) {
    console.error(`  ${name} 抛错变化：${baseline.failures[name] ?? '(无)'} -> ${actual.failures[name] ?? '(无)'}`);
  }
  process.exit(1);
}

console.log(`schema 对拍通过：${Object.keys(actual.result).length} 个用例产出与基线一致`);
