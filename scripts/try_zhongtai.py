"""不启动模型、不改业务库，先把这次补上的合同跑一遍。

用法（在 webpage-agent-demo 目录）：

    python -m scripts.try_zhongtai

通过之后，按脚本末尾打印的 curl 例子对正在运行的中台再测一遍。
"""
import sys
import time
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import jwt

from app import auth_connections, scene_context, ticket_auth
from app.integrations import mcp_adapter
from tests.test_ticket_auth import ISS, PRIVATE_A, connection, sign


def _check_ticket() -> None:
    conn = connection()
    with patch.object(ticket_auth.auth_connections, 'connections',
                      return_value=[conn]):
        try:
            ticket_auth.verify(sign(kid='wrong-kid'))
        except ticket_auth.TicketError as exc:
            print(f'  1. 错误 kid 被拒绝：{exc.reason}')
        else:
            raise SystemExit('错误 kid 不应通过')
        raw = sign()
        header, payload, signature = raw.split('.')
        import base64
        import json
        padded = header + '=' * (-len(header) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        data.pop('typ', None)
        encoded = base64.urlsafe_b64encode(
            json.dumps(data, separators=(',', ':')).encode()).rstrip(b'=').decode()
        try:
            ticket_auth.verify(f'{encoded}.{payload}.{signature}')
        except ticket_auth.TicketError as exc:
            print(f'  2. 缺少 typ 被拒绝：{exc.reason}')
        else:
            raise SystemExit('缺少 typ 不应通过')


def _check_scene() -> None:
    context = {'project_id': 'project_001', 'selection': [{'type': 'scene', 'id': 'scene_003'}]}
    now = int(time.time())
    token = jwt.encode(
        {'iss': ISS, 'aud': scene_context.SCENE_AUDIENCE, 'sub': 'user-123',
         'tenant_id': 'tenant-a', 'platform_id': 'pa',
         'ctx': scene_context.canonical_hash(context),
         'iat': now, 'exp': now + 60},
        PRIVATE_A, algorithm='RS256',
        headers={'kid': 'k1', 'typ': scene_context.SCENE_TYPE})
    conn = connection()
    with patch.object(auth_connections, 'connections', return_value=[conn]):
        ok = scene_context.verify(token, context, tenant_id='tenant-a')
        print(f'  3. 签名现场通过：project={ok["context"]["project_id"]}')
        try:
            scene_context.verify(token, {'project_id': 'other'}, tenant_id='tenant-a')
        except scene_context.SceneError as exc:
            print(f'  4. 篡改现场被拒绝：{exc.reason}')
        else:
            raise SystemExit('篡改的现场不应通过')


def _check_token_isolation() -> None:
    explicit = mcp_adapter.auth_headers('tenant-secret', 'global-secret')
    empty = mcp_adapter.auth_headers('', 'global-secret')
    inherited = mcp_adapter.auth_headers(None, 'global-secret')
    assert explicit['Authorization'] == 'Bearer tenant-secret'
    assert empty is None
    assert inherited['Authorization'] == 'Bearer global-secret'
    print('  5. 显式租户钥匙不会退回全局 token')


def main() -> int:
    print('中台合同自检（不调用模型，不写数据库）')
    _check_ticket()
    _check_scene()
    _check_token_isolation()
    print('自检通过。')
    print()
    print('下面五段可以对着已启动的中台再跑。先在另一个终端：')
    print('  cd webpage-agent-demo')
    print('  python -m scripts.serve_api --port 8010')
    print()
    print('# 例 1  健康检查')
    print("curl -s localhost:8010/api/health")
    print()
    print('# 例 2  本地单用户建一个任务（会调用模型，需要 .env 里的 LLM 与数据库）')
    print("curl -s -X POST localhost:8010/api/tasks \\")
    print("  -H 'Content-Type: application/json' \\")
    print("  -d '{\"message\":\"用一句话说明 SQLite 和 PostgreSQL 的差别\"}'")
    print()
    print('# 例 3  登记本企业的服务端、凭据引用和政策')
    print("# AUTH_MODE=off 时主体是 default，且 .env 里 AUTH_OPERATORS 含 default")
    print("curl -s -X POST localhost:8010/api/admin/server-bindings \\")
    print("  -H 'Content-Type: application/json' \\")
    print("  -d '{\"tenant_id\":\"default\",\"server_id\":\"comic\",\"transport\":\"streamable_http\"}'")
    print("curl -s -X POST localhost:8010/api/admin/credential-bindings \\")
    print("  -H 'Content-Type: application/json' \\")
    print("  -d '{\"tenant_id\":\"default\",\"server_id\":\"comic\",\"credential_ref\":\"env:COMIC_TOKEN\"}'")
    print("curl -s -X POST localhost:8010/api/admin/tool-policies \\")
    print("  -H 'Content-Type: application/json' \\")
    print("  -d '{\"tenant_id\":\"default\",\"tool_key\":\"mcp:comic:update_scene\",\"approval_mode\":\"required\"}'")
    print()
    print('# 例 4  ticket 模式下，现场上下文被改过会 400。先按 README 用模拟平台换好会话，再：')
    print("curl -s -X POST localhost:8010/api/tasks \\")
    print("  -H 'Content-Type: application/json' \\")
    print("  -b 'agent_session=换到的会话' \\")
    print("  -d '{\"message\":\"把选中分镜缩短\",\"context\":{\"project_id\":\"p1\"},\"context_assertion\":\"not-a-jwt\"}'")
    print()
    print('# 例 5  任务跑到等待批准后，用返回的 approval_id 和 manifest_hash 确认')
    print("curl -s localhost:8010/api/runs/<run_id>/approvals")
    print("curl -s -X POST localhost:8010/api/approvals/<approval_id>/approve \\")
    print("  -H 'Content-Type: application/json' \\")
    print("  -d '{\"manifest_hash\":\"<manifest_hash>\"}'")
    print()
    print('结果信封和异步查询默认关闭。要试这两条，启动前加上：')
    print('  RESULT_ENVELOPE_V2=true ASYNC_TOOL_RUNTIME_ENABLED=true')
    print('并另开一个 Worker：python -m scripts.run_worker')
    print('合同样例在 contracts/async_tools.example.json，复制为 async_tools.json 后才会按它查询。')
    print('MCP 成功后可看 GET /api/results/<result_id>')
    print('外部任务可看 GET /api/runs/<run_id>/external-tasks')
    print('批准确认后响应里的 resumed=true 表示同一次运行已重新排队。')
    print()
    print('# 例 6  存活与就绪')
    print('curl -s localhost:8010/api/health/live')
    print('curl -s localhost:8010/api/health/ready')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
