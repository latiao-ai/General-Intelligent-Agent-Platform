"""端到端校验：连了 beeftv 的会话，读要走通、写要先确认。

这条链路真踩过三个坑，全都表现为「调用看起来没事、结果全错」：

1. 工具被全局白名单拒 → 模型收到「工具未开放」；
2. 工具调通了，但回执只认 `documents`，MCP 的平台对象（projects）被丢掉 →
   模型收到 `sources: []`，回答「0 个画布」；
3. 写工具被"确认"挡了一道，但用户点完确认仍是拒绝 → 白点一次。

所以脚本不测内部函数，只从**用户入口**发消息，然后对照三份事实：

- `session_turns`：这一轮跑完没有、答复是什么；
- `session_turn_events`：调用了哪些工具、有没有弹"待确认"；
- `actions`：工具层到底拿回了什么、写操作到底执行了没有。

用法（服务与 Worker 都在跑）：

    python -m scripts.oneoff.e2e_mcp_canvas_check             # 读：问画布数量
    python -m scripts.oneoff.e2e_mcp_canvas_check --write     # 写：要求建节点，应停在确认
    python -m scripts.oneoff.e2e_mcp_canvas_check --keep      # 保留测试会话，便于在界面里看

写模式**不会真的改动画布**：它验证的是"在执行前停下来问确认"，因此断言
"没有任何写 action 成功结算"。默认跑完删掉测试会话（账本/审计行保留）。
"""
import argparse
import asyncio
import hashlib
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_ROOT / 'src'))

BASE = 'http://127.0.0.1:8002'
COOKIE = 'agent_session'
DSN = 'postgresql://latiao@127.0.0.1:5432/agent_platform'
BEETV_SUBJECT = 'cac4086e32f61745cb6b1929c0cc6333'
CANVAS_ID = 'r7-mLttPVxOrBDhPk4MFew'   # “中台联调画布A”，联调用画布
TOKEN = ''      # 该 beeftv 账号在中台的主体
READ_ASK = '我现在的 beeftv 账号下有几个画布？只回答数量和标题，不要做别的操作。'
WRITE_ASK = ('在名为“中台联调画布A”的画布里新建一个图片节点，提示词是“海边日落”，'
             '节点标题叫“E2E 校验节点”。')
TERMINAL = ('succeeded', 'failed', 'cancelled', 'waiting_clarification')


def psql(sql: str) -> str:
    done = subprocess.run(['psql', DSN, '-tA', '-q', '-c', sql],
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f'psql 失败：{done.stderr.strip()}')
    return done.stdout.strip()


def mint_login(token: str, user_id: str) -> str:
    digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
    now = time.strftime('%Y-%m-%dT%H:%M:%S.000000+00:00', time.gmtime())
    sid = 'as_e2e_' + secrets.token_hex(6)
    psql('INSERT INTO auth_sessions (auth_session_id, token_digest, user_id, '
         'tenant_id, platform_id, connection_id, identity_provider_id, subject, '
         'issued_at, expires_at, last_seen_at, user_agent, remote_addr) VALUES '
         f"('{sid}', '{digest}', '{user_id}', 'default', 'beeftv', '', 'beeftv', "
         f"'{BEETV_SUBJECT}', '{now}', {time.time() + 1800}, '{now}', "
         "'e2e-check', '127.0.0.1')")
    return sid


async def chat(client: httpx.AsyncClient, session_id: str, message: str) -> str:
    body = {'request_id': 'e2e-' + secrets.token_hex(6),
            'message': message, 'connector_ids': ['beeftv']}
    res = await client.post(f'{BASE}/api/sessions/{session_id}/messages', json=body)
    if res.status_code != 202:
        raise SystemExit(f'接纳失败 {res.status_code}: {res.text[:300]}')
    turn_id = res.json().get('turn_id')
    print(f'  接纳 202，turn={turn_id}')

    deadline = time.time() + 150
    while time.time() < deadline:
        await asyncio.sleep(1.0)
        timeline = (await client.get(f'{BASE}/api/sessions/{session_id}/timeline')).json()
        turn = next((t for t in timeline.get('chat_turns') or []
                     if t.get('turn_id') == turn_id), None)
        if not turn:
            continue
        if str(turn.get('status')) in TERMINAL:
            return turn_id
        draft = (turn.get('draft') or '').strip()
        if draft:
            print(f'  …生成中（{len(draft)} 字）')
    raise SystemExit('等待 turn 终态超时（150s）')


def tool_calls(turn_id: str) -> list[str]:
    raw = psql("SELECT event_json FROM session_turn_events WHERE turn_id = "
               f"'{turn_id}' ORDER BY id")
    names = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('type') == 'tool_call':
            names.append(str(event.get('name') or ''))
    return names


def confirmation_events(turn_id: str) -> int:
    raw = psql("SELECT count(*) FROM session_turn_events WHERE turn_id = "
               f"'{turn_id}' AND event_json LIKE '%tool_confirmation_required%'")
    return int(raw or 0)


def _projects_len(value, depth: int = 0) -> int:
    """在 payload 里递归找 projects 列表的长度。

    不同工具的外壳不一样：`business_get_canvas_projects` 直接在 payload.projects，
    `canvas.search` 在 payload.result.projects。别把校验写死在某一种形状上。
    """
    if depth > 6:
        return 0
    if isinstance(value, dict):
        found = 0
        for key, item in value.items():
            if key == 'projects' and isinstance(item, list):
                found = max(found, len(item))
            else:
                found = max(found, _projects_len(item, depth + 1))
        return found
    if isinstance(value, list):
        return max((_projects_len(item, depth + 1) for item in value), default=0)
    return 0


def delivered_projects(turn_id: str) -> int:
    """这一轮里工具真实拿回多少条画布（以 action.receipt 为准，不看模型怎么说）。"""
    raw = psql("SELECT receipt_json FROM actions WHERE run_id = "
               f"'{turn_id}' AND status = 'succeeded' ORDER BY created_at")
    best = 0
    for line in raw.splitlines():
        try:
            receipt = json.loads(line)
        except ValueError:
            continue
        for call in receipt.get('mcp_calls') or []:
            best = max(best, _projects_len(call.get('payload')))
    return best


def successful_writes(turn_id: str) -> int:
    raw = psql("SELECT count(*) FROM actions WHERE run_id = "
               f"'{turn_id}' AND status = 'succeeded' AND effect <> 'read'")
    return int(raw or 0)


def cleanup(session_id: str, turn_id: str, auth_sid: str) -> None:
    """按**会话**清理（confirm 模式有两轮 turn，不能只删一条）。"""
    psql('DELETE FROM session_turn_events WHERE turn_id IN (SELECT turn_id '
         f"FROM session_turns WHERE session_id = '{session_id}')")
    psql(f"DELETE FROM session_turns WHERE session_id = '{session_id}'")
    psql(f"DELETE FROM messages WHERE session_id = '{session_id}'")
    psql(f"DELETE FROM sessions WHERE session_id = '{session_id}'")
    psql(f"DELETE FROM auth_sessions WHERE auth_session_id = '{auth_sid}'")



async def _mcp_call(name: str, args: dict) -> dict:
    """以该 beeftv 身份直调 MCP（只用于校验与清理，不经过智能体）。"""
    from webpage_agent.integrations import mcp_servers
    from webpage_agent.integrations.mcp_adapter import call_tool
    from scripts.list_mcp_tools import _probe_context

    server = next(s for s in mcp_servers.servers() if s.id == 'beeftv')
    with _probe_context(BEETV_SUBJECT):
        return await call_tool(name, args, server=server)


def _find(value, key: str, depth: int = 0):
    """在 MCP 返回的嵌套信封里找第一个 key（画布/节点列表的外壳各版本不一致）。"""
    if depth > 6:
        return None
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for item in value.values():
            found = _find(item, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find(item, key, depth + 1)
            if found is not None:
                return found
    return None


async def confirm_flow(session_id: str) -> list[str]:
    """两轮：先让它给方案，再回一句"确认"，看它到底执行没有。"""
    problems = []
    async with httpx.AsyncClient(timeout=30, cookies={COOKIE: TOKEN}) as client:
        await chat(client, session_id, WRITE_ASK)
        turn_id = await chat(client, session_id, '确认，执行吧')

    status = psql(f"SELECT status FROM session_turns WHERE turn_id = '{turn_id}'")
    calls = tool_calls(turn_id)
    writes = successful_writes(turn_id)
    answer = psql(f"SELECT answer FROM session_turns WHERE turn_id = '{turn_id}'")
    print('\n第二轮（用户回“确认”）')
    print(f'  turn 状态    : {status}')
    print(f'  工具调用     : {", ".join(calls) or "（无）"}')
    print(f'  写 action    : {writes} 条成功结算（应 > 0）')
    print(f'  答复         :\n{answer.strip()[:400]}\n')
    if writes <= 0:
        problems.append('用户确认之后仍然没有写入（确认没生效或模型没重放工具）')

    # 真的落到画布上了吗？直接读一次
    canvas = await _mcp_call('canvas.get', {'canvasId': CANVAS_ID})
    nodes = _find(canvas, 'nodes') or []
    created = [n for n in nodes if 'E2E' in str(n.get('title') or '')]
    revision = _find(canvas, 'revision')
    print(f'  画布现状     : revision={revision}，节点 {len(nodes)} 个，'
          f'其中 E2E 节点 {len(created)} 个')

    # 清理：删掉刚建的节点，把画布还原
    for node in created:
        try:
            await _mcp_call('canvas.node.delete', {
                'canvasId': CANVAS_ID,
                'expectedRevision': int(revision or 0),
                'nodeId': str(node.get('id') or ''),
                # 写工具要稳定的幂等键，缺了 schema 直接拒。
                'operationId': 'e2e-cleanup-' + secrets.token_hex(4),
            })
            revision = int(revision or 0) + 1
        except Exception as exc:                       # noqa: BLE001
            print(f'  ⚠ 清理节点失败（请手动删）：{exc}')
    after = await _mcp_call('canvas.get', {'canvasId': CANVAS_ID})
    left = [n for n in (_find(after, 'nodes') or [])
            if 'E2E' in str(n.get('title') or '')]
    print(f'  清理结果     : 残留 E2E 节点 {len(left)} 个')
    if left:
        problems.append('测试节点没清掉，请在画布上手动删除')
    return problems


async def run_mode(mode: str) -> int:
    global TOKEN
    session_id = 's_e2e_' + secrets.token_hex(4)
    TOKEN = 'e2e-' + secrets.token_urlsafe(24)
    user_id = psql("SELECT user_id FROM identities WHERE provider = 'beeftv' "
                   f"AND subject = '{BEETV_SUBJECT}'")
    if not user_id:
        raise SystemExit('找不到 beeftv 身份对应的中台用户，先登录一次再跑')
    auth_sid = mint_login(TOKEN, user_id)
    print(f'测试会话 {session_id}（用户 {user_id}，模式 {mode}）')

    if mode == 'confirm':
        async with httpx.AsyncClient(timeout=30) as client:
            client.cookies.set(COOKIE, TOKEN)
            me = await client.get(f'{BASE}/api/me')
            if me.status_code != 200:
                raise SystemExit(f'登录态无效 {me.status_code}: {me.text[:200]}')
        problems = await confirm_flow(session_id)
        cleanup(session_id, '', auth_sid)
        print('已清理测试会话与测试节点（账本/审计行保留）')
        if problems:
            print('❌ 未通过：' + '；'.join(problems))
            return 1
        print('✅ 通过：确认之后真的写入了，并且已清理')
        return 0

    async with httpx.AsyncClient(timeout=30, cookies={COOKIE: TOKEN}) as client:
        me = await client.get(f'{BASE}/api/me')
        if me.status_code != 200:
            raise SystemExit(f'登录态无效 {me.status_code}: {me.text[:200]}')
        turn_id = await chat(client, session_id,
                             WRITE_ASK if mode == 'write' else READ_ASK)

    answer = psql(f"SELECT answer FROM session_turns WHERE turn_id = '{turn_id}'")
    status = psql(f"SELECT status FROM session_turns WHERE turn_id = '{turn_id}'")
    calls = tool_calls(turn_id)
    confirmations = confirmation_events(turn_id)

    print('\n结果')
    print(f'  turn 状态    : {status}')
    print(f'  工具调用     : {", ".join(calls) or "（无）"}')
    print(f'  待确认事件   : {confirmations} 次')
    problems = []
    if status != 'succeeded':
        problems.append(f'turn 未成功（{status}）')

    if mode == 'read':
        real = delivered_projects(turn_id)
        print(f'  工具真实拿回 : {real} 个画布（以 action.receipt 为准）')
        print(f'  答复         :\n{answer.strip()[:500]}\n')
        if real <= 0:
            problems.append('工具层没拿回画布，先查 MCP 连接/身份')
        if not any('canvas' in c or 'projects' in c for c in calls):
            problems.append('这一轮没有调用画布类工具')
        if real > 0 and str(real) not in answer:
            problems.append(f'答复没体现真实数量 {real}（可能又是结果没传给模型）')
        if not problems:
            print(f'✅ 通过：工具拿回 {real} 个画布，答复与工具结果一致')
    else:
        writes = successful_writes(turn_id)
        write_verbs = ('create', 'update', 'delete', 'submit', 'generate',
                       'rename', 'apply', 'export')
        write_calls = [c for c in calls
                       if any(verb in c.lower() for verb in write_verbs)]
        print(f'  写工具调用   : {", ".join(write_calls) or "（无，模型选择先问）"}')
        print(f'  写 action    : {writes} 条成功结算（应为 0）')
        print(f'  待确认事件   : {confirmations} 次（harness 在执行口拦下）')
        print(f'  答复         :\n{answer.strip()[:500]}\n')
        if writes:
            problems.append(f'{writes} 条写操作在未确认时执行了')
        if '确认' not in answer:
            problems.append('答复里没有向用户要确认')
        if write_calls and not confirmations:
            problems.append('模型调了写工具，但既没被 harness 拦下、也没向用户确认')
        if not problems:
            via = 'harness 拦截' if confirmations else '模型先询问'
            print(f'✅ 通过：写操作停在确认这一步、未落库（{via}）')

    print('已清理测试会话（账本/审计行保留）')
    cleanup(session_id, turn_id, auth_sid)
    if problems:
        print('❌ 未通过：' + '；'.join(problems))
        return 1
    return 0


async def main() -> int:
    global BASE
    parser = argparse.ArgumentParser()
    parser.add_argument('--write', action='store_true',
                        help='校验写操作先确认（不会真的改动画布）')
    parser.add_argument('--confirm', action='store_true',
                        help='校验"确认后真的执行"（会在测试画布上建节点并清理）')
    parser.add_argument('--keep', action='store_true', help='保留测试会话')
    parser.add_argument('--base', default=BASE, help='服务地址（默认 8002）')
    args = parser.parse_args()
    BASE = args.base.rstrip('/')
    mode = 'confirm' if args.confirm else ('write' if args.write else 'read')
    return await run_mode(mode)


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
