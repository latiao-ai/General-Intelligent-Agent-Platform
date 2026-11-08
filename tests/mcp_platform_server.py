"""第十阶段：隔离测试平台 MCP 服务端（stdio transport）。

提供「可核验的最小平台合同」三个工具：

- `get_object`：按对象 ID 读取对象（含 owner、revision、字段值）。只读。
- `update_object`：按对象 ID 修改字段。写参数含对象 ID、预期 revision、幂等键
  （operation_id）与修改字段。服务端**原子**执行归属校验、revision 比较、幂等
  去重；同键不同参数拒绝。返回新的 revision 与 operation_id。
- `get_operation`：按操作 ID 读取操作记录（含对象、写入方、预期/新 revision、
  幂等键、修改字段）。用于「远端已写、本地未记账」窗口的恢复核实。

对象归属、revision、操作记录**持久化到 JSON 文件**（`--state-file`），服务重启
后仍可查询——这是「可核验」的关键：测试端跨进程读同一份状态，不依赖进程内内存。

合成主体：`update_object` 参数带 `subject`（合成 tenant/user 标识），服务端据此
记录归属；`owner` 与 `subject` 不一致时拒绝写入（对象级隔离的服务端侧校验）。

计数：`actual_writes` 记录**真实写入次数**（不是本地 action 数），测试端据此
断言「越权/revision 冲突时无实际写入」。

用法：
    python -m tests.mcp_platform_server --state-file /tmp/platform.json

协议：stdio。对象初始状态可由 `--seed` 提供（JSON 文件），测试端用它预置
「主体 A 的对象」与「主体 B 的对象」。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import Server

# 对象修改的幂等键参数名（与宿主侧 execution_arguments 注入的 idempotency_arg
# 对应；这里用 operation_id 作为业务幂等键）。
IDEMPOTENCY_ARG = 'operation_id'


class _Store:
    """文件持久化的平台状态（跨进程一致）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict = {'objects': {}, 'operations': {}, 'actual_writes': 0}
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        if self.path.is_file():
            try:
                self.data = json.loads(self.path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                self.data = {'objects': {}, 'operations': {}, 'actual_writes': 0}
        self.data.setdefault('objects', {})
        self.data.setdefault('operations', {})
        self.data.setdefault('actual_writes', 0)

    def _flush(self) -> None:
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding='utf-8')
        tmp.replace(self.path)


def read_state(path: str | Path) -> dict:
    """测试端跨进程读取平台状态。"""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def seed_state(path: str | Path, objects: dict, operations: list | None = None) -> None:
    """测试端预置对象初始状态（主体 A/B 各持一个对象）。"""
    p = Path(path)
    data = {'objects': dict(objects), 'operations': {}, 'actual_writes': 0}
    for op in operations or []:
        data['operations'][op['operation_id']] = op
    p.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def build_server(state_file: str) -> Server:
    store = _Store(Path(state_file))
    server = Server('platform')

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name='get_object',
                description='读取一个平台对象（owner、revision、字段值）。只读。',
                inputSchema={'type': 'object',
                             'properties': {'object_id': {'type': 'string'}},
                             'required': ['object_id']},
                annotations=types.ToolAnnotations(readOnlyHint=True)),
            types.Tool(
                name='update_object',
                description='修改一个平台对象字段。需对象ID、预期revision、幂等键与修改字段；'
                            '服务端原子校验归属与revision，幂等去重。',
                inputSchema={'type': 'object',
                             'properties': {
                                 'object_id': {'type': 'string'},
                                 'expected_revision': {'type': 'integer'},
                                 'operation_id': {'type': 'string'},
                                 'subject': {'type': 'string'},
                                 'fields': {'type': 'object',
                                            'additionalProperties': True},
                             },
                             'required': ['object_id', 'expected_revision',
                                          'operation_id', 'subject', 'fields']},
                annotations=types.ToolAnnotations(readOnlyHint=False,
                                                  destructiveHint=False,
                                                  idempotentHint=True)),
            types.Tool(
                name='get_operation',
                description='按操作ID读取操作记录（对象、写入方、revision、幂等键、字段）。只读。',
                inputSchema={'type': 'object',
                             'properties': {'operation_id': {'type': 'string'}},
                             'required': ['operation_id']},
                annotations=types.ToolAnnotations(readOnlyHint=True)),
        ]

    def _error(code: str, message: str) -> list[types.TextContent]:
        return [types.TextContent(
            type='text',
            text=json.dumps({'status': 'error', 'code': code,
                             'message': message}, ensure_ascii=False))]

    def _object_payload(obj: dict) -> dict:
        return {'object_id': obj['object_id'], 'owner': obj['owner'],
                'revision': obj['revision'], 'fields': obj['fields']}

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        async with store._lock:
            if name == 'get_object':
                obj_id = str(arguments.get('object_id') or '')
                obj = store.data['objects'].get(obj_id)
                if obj is None:
                    return _error('E_NOT_FOUND', f'对象不存在：{obj_id}')
                return [types.TextContent(
                    type='text',
                    text=json.dumps({'status': 'ok',
                                     **_object_payload(obj)}, ensure_ascii=False))]

            if name == 'update_object':
                obj_id = str(arguments.get('object_id') or '')
                expected = arguments.get('expected_revision')
                op_id = str(arguments.get(IDEMPOTENCY_ARG) or '')
                subject = str(arguments.get('subject') or '')
                fields = arguments.get('fields') or {}

                obj = store.data['objects'].get(obj_id)
                if obj is None:
                    return _error('E_NOT_FOUND', f'对象不存在：{obj_id}')
                # 对象级隔离：归属校验（服务端侧）
                if obj.get('owner') != subject:
                    return _error('E_FORBIDDEN',
                                  f'对象 {obj_id} 不属于 {subject}，拒绝写入')
                if not isinstance(expected, int):
                    return _error('E_BAD_REQUEST', 'expected_revision 必须是整数')

                # 幂等去重：同操作ID已存在 → 检查参数是否一致
                existing = store.data['operations'].get(op_id)
                if existing is not None:
                    same = (existing.get('object_id') == obj_id
                            and existing.get('expected_revision') == expected
                            and existing.get('subject') == subject
                            and existing.get('fields') == fields)
                    if not same:
                        return _error('E_IDEMPOTENCY_CONFLICT',
                                      f'幂等键 {op_id} 已用于不同参数，拒绝')
                    # 同键同参数：幂等重放，返回原操作结果，不重复写
                    return [types.TextContent(
                        type='text',
                        text=json.dumps({'status': 'ok', 'replayed': True,
                                         **existing.get('result', {})},
                                        ensure_ascii=False))]

                # revision 比较：预期 revision 必须等于当前 revision
                current = int(obj.get('revision') or 0)
                if expected != current:
                    return _error('E_REVISION_CONFLICT',
                                  f'预期 revision {expected} 与当前 {current} 不符，拒绝')

                # 实际写入：revision +1，合并字段，记录操作，计数
                obj['revision'] = current + 1
                obj['fields'] = {**obj.get('fields', {}), **fields}
                store.data['actual_writes'] = int(store.data.get('actual_writes') or 0) + 1
                result = {'object_id': obj_id, 'new_revision': obj['revision']}
                store.data['operations'][op_id] = {
                    'operation_id': op_id, 'object_id': obj_id,
                    'subject': subject, 'expected_revision': expected,
                    'new_revision': obj['revision'], 'fields': fields,
                    'result': result, 'at': time.time(),
                }
                store._flush()
                return [types.TextContent(
                    type='text',
                    text=json.dumps({'status': 'ok', **result},
                                    ensure_ascii=False))]

            if name == 'get_operation':
                op_id = str(arguments.get('operation_id') or '')
                op = store.data['operations'].get(op_id)
                if op is None:
                    return _error('E_NOT_FOUND', f'操作不存在：{op_id}')
                return [types.TextContent(
                    type='text',
                    text=json.dumps({'status': 'ok', 'operation': op},
                                    ensure_ascii=False))]

            return _error('E_UNKNOWN_TOOL', f'未知工具：{name}')

    return server


async def _run(state_file: str) -> None:
    from mcp.server.stdio import stdio_server
    server = build_server(state_file)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser(description='隔离测试平台 MCP 服务端')
    parser.add_argument('--state-file', required=True, help='平台状态文件路径')
    args = parser.parse_args()
    asyncio.run(_run(args.state_file))


if __name__ == '__main__':
    main()
