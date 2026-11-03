"""支持「受理 → 查询」异步合同的最小 mock MCP 服务端（仅用于链路验证）。

它刻意模拟**方言型**服务端（不是标准 MCP Tasks）：

- `submit_job` 立刻返回 `{"status": "queued", "job_id": ...}` —— 不是最终结果；
- `get_job_status(job_id)` 按任务号查询，状态依次推进
  `queued → running → completed`（每次查询推进一格）。

只靠这两点，就足以让中台的**异步合同**（`contracts/async_tools.json` 那一套）
把它当成一个"已受理的长任务"来跟踪，走
`acceptance → remember_waiting → poll_due → _wake_run` 全链路。

启动::

    python -m scripts.mock_async_mcp_server        # 127.0.0.1:8766

`MCP_SERVERS` 里对应的登记项::

    {"id": "mockjob", "transport": "streamable_http",
     "url": "http://127.0.0.1:8766/mcp"}
"""
import uuid

from mcp.server.fastmcp import FastMCP

HOST = '127.0.0.1'
PORT = 8766

mcp = FastMCP('mock-async-job', host=HOST, port=PORT)

# job_id -> {'polls': n, 'status': str}。进程内存储：重启即丢，
# 这正好复刻"服务端没有持久任务号"这类真实方言的弱点。
JOBS: dict[str, dict] = {}


@mcp.tool()
def submit_job(payload: str = 'demo') -> dict:
    """提交一个长任务，**立刻**返回任务号，不等它跑完。

    Args:
        payload: 任务内容（这里只回显，不做实际工作）。
    """
    job_id = f'job-{uuid.uuid4().hex[:8]}'
    JOBS[job_id] = {'polls': 0, 'status': 'queued', 'payload': payload}
    return {
        'status': 'queued',
        'job_id': job_id,
        'tool_name': 'submit_job',
        'next_action': 'poll_get_job_status',
    }


@mcp.tool()
def get_job_status(job_id: str) -> dict:
    """按任务号查询状态。每次查询推进一格：queued → running → completed。

    Args:
        job_id: `submit_job` 返回的任务号。
    """
    job = JOBS.get(job_id)
    if job is None:
        return {'status': 'error', 'code': 'E_RESOURCE_NOT_FOUND',
                'message': f'job {job_id} not found'}
    job['polls'] += 1
    if job['polls'] >= 3:
        job['status'] = 'completed'
    elif job['polls'] >= 1:
        job['status'] = 'running'
    return {
        'job_id': job_id,
        'status': job['status'],
        'polls': job['polls'],
        'artifact': 'output.txt' if job['status'] == 'completed' else None,
    }


@mcp.tool()
def cancel_job(job_id: str) -> dict:
    """取消任务（协作式：已经 completed 的不再改变）。

    Args:
        job_id: 任务号。
    """
    job = JOBS.get(job_id)
    if job is None:
        return {'status': 'error', 'code': 'E_RESOURCE_NOT_FOUND'}
    if job['status'] != 'completed':
        job['status'] = 'cancelled'
    return {'job_id': job_id, 'status': job['status']}


if __name__ == '__main__':
    mcp.run(transport='streamable-http')
