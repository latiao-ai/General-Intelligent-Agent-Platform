import argparse
import asyncio
import json
# 直接跑脚本时（python scripts/xxx.py）sys.path[0] 是 scripts/，项目根不在其中，
# 导入 app.* 会失败。这里显式补上，让"直接跑"和"python -m scripts.xxx"都能用。
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

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
