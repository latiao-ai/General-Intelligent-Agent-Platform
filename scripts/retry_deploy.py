import argparse
import asyncio
import json
from app.main import deploy_task

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('task_id')
    parser.add_argument('--server-state-checked', action='store_true',
                        help='已核对服务端状态并确认可以调用部署')
    args = parser.parse_args()
    if not args.server_state_checked:
        parser.error('先查询服务端是否已部署，再加 --server-state-checked')
    # deploy_task 会校验 DEPLOY_ENABLED、task_id 合法性与 HTML 哈希，再执行部署。
    result = await deploy_task(args.task_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    asyncio.run(main())
