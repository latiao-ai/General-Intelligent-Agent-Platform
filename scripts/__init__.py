"""开发与运维入口脚本。

`python -m scripts.xxx` 会先执行本模块：把仓库的 `src/` 加进 `sys.path`，
这样不需要预先 `export PYTHONPATH=src` 也能导入 `webpage_agent`。

测试**不走**这条路——`scripts/run_tests.sh` 与 CI 都显式设置 `PYTHONPATH=src`，
让用例必须通过真实的包路径导入，而不是靠当前目录碰巧在 `sys.path` 里。
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / 'src'
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
