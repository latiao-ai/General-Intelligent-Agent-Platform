"""`webpage_agent.integrations.mcp_servers` 必须自己触发 `.env` 加载。

这个模块只读 `os.environ`，不读 `settings`。`.env` 的加载是 `webpage_agent.core.config`
导入时的副作用。少了那一步导入，先 import 本模块的脚本会读到空环境，
解析出 0 个服务端，而 `LAST_ERROR` 仍是空串——静默为空，最难查。

**夹具假设**：测试复制**整个 `app/` 包**到临时目录（而不是挑两个文件），
这样 `mcp_servers` 以后新增任何 app 内导入都能被解析。若仍撞上 ImportError，
子进程用 `IMPORT_ERROR:` 前缀单独报出来——"夹具覆盖不到新依赖"和
"不变量被破坏"是两回事，失败形态不能混在一起。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_PROBE_CODE = (
    'import sys\n'
    'try:\n'
    '    from webpage_agent.integrations import mcp_servers\n'
    'except ImportError as exc:\n'
    '    print(f"IMPORT_ERROR: {exc}", file=sys.stderr)\n'
    '    raise SystemExit(2)\n'
    'ids = [server.id for server in mcp_servers.servers()]\n'
    'if ids != ["probe"]:\n'
    '    print(f"INVARIANT_BROKEN: .env 未加载，解析出 {ids}", file=sys.stderr)\n'
    '    raise SystemExit(3)\n'
)


class McpServersEnvLoadingTests(unittest.TestCase):
    def _run_in_copied_tree(self, with_env_file: bool):
        """复制真实 app/ 包到临时目录，再只 import mcp_servers。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'src').mkdir(parents=True, exist_ok=True)
            shutil.copytree(ROOT / 'src' / 'webpage_agent', root / 'src' / 'webpage_agent',
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            if with_env_file:
                (root / '.env').write_text(
                    'MCP_SERVERS=[{"id": "probe", "url": "https://probe.example/mcp"}]\n',
                    encoding='utf-8')
            return subprocess.run(
                [sys.executable, '-c', _PROBE_CODE],
                cwd=root,
                env={'APP_ENV': 'development', 'PATH': os.environ.get('PATH', ''),
                     'PYTHONPATH': str(root / 'src')},
                capture_output=True, text=True)

    def test_importing_mcp_servers_alone_reads_dotenv(self):
        """只 import mcp_servers 时，`.env` 里的 MCP_SERVERS 必须被读到。"""
        completed = self._run_in_copied_tree(with_env_file=True)
        # 依赖缺失不是这条用例要证明的事，单独报出来，别伪装成"不变量被破坏"。
        self.assertNotIn('IMPORT_ERROR', completed.stderr,
                         f'mcp_servers 新增了 app 内依赖但夹具没覆盖：{completed.stderr}')
        self.assertNotIn('INVARIANT_BROKEN', completed.stderr, completed.stderr)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_no_env_file_contributes_no_ops_server(self):
        """没有 `.env` 时不应凭空多出运维服务端，也不应报错。

        这里不再断言"整张表为空"：`servers()` 现在无论如何都会合并平台目录
        （控制台发布的连接器）——以前 `MCP_SERVERS` 为空会提前 return，把平台
        连接器和用户自建的连接器一起跳过，那是 bug 而不是不变量。这条用例要
        证明的是"环境没加载"，所以只要求 `probe` 不出现、`LAST_ERROR` 为空。
        """
        completed = subprocess.run(
            [sys.executable, '-c',
             'from webpage_agent.integrations import mcp_servers\n'
             'ids = [s.id for s in mcp_servers.servers()]\n'
             'assert "probe" not in ids, ids\n'
             'assert not mcp_servers.LAST_ERROR, mcp_servers.LAST_ERROR\n'],
            cwd=ROOT,
            env={'APP_ENV': 'production', 'PATH': os.environ.get('PATH', ''),
                 'PYTHONPATH': str(ROOT / 'src')},
            capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
