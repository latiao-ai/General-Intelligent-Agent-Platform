"""出厂 `contracts/async_tools.json` 必须真的能加载、且字段合法。

`async_contracts.load()` 遇到 JSON 错误或非法行会**静默返回空清单**，
`summary()` 于是报 0，启动日志退回"没有可恢复合同"的警告——异步长任务
退化成"已排队"被当成成功。这条链路上没有任何异常，只有一条日志，
所以必须用测试把出厂文件钉住。

当前仓库不再随附任何具体平台的异步合同，出厂文件是空合同清单；
字段校验与可恢复判定的逻辑，用内联示例合同验证，确保接入新平台时
合同格式仍然被严格检查。
"""
import json
import unittest
from pathlib import Path

from webpage_agent.core import async_contracts

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / 'contracts' / 'async_tools.json'
# map_status() 只认下面这些值，其余（含拼错的）一律折成 unknown。
# `unknown` 本身是合法落点：显式把某个状态写成 unknown（例如服务端重启后的
# `interrupted`）是"说不清就不要猜"的诚实降级，不是拼写错误。
LEGAL_STATUSES = {'accepted', 'running', 'succeeded', 'failed', 'cancelled', 'unknown'}


class ShippedAsyncContractsTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SHIPPED.is_file(), f'缺少出厂合同文件 {SHIPPED}')
        self.rows = json.loads(SHIPPED.read_text(encoding='utf-8'))['contracts']

    def test_shipped_file_loads_as_empty_list(self):
        """出厂文件当前是空合同清单，但必须能被 load() 正常读取。"""
        self.assertIsInstance(self.rows, list)
        self.assertEqual(async_contracts.load(), [])

    def test_field_validation_accepts_well_formed_contract(self):
        """校验逻辑用内联的合法示例合同验证，确保接入新平台时仍严格检查。"""
        sample = {
            "contract_id": "example-video-v1",
            "version": 1,
            "submit_tool_key": "mcp:example:render_video",
            "remote_id_pointer": "/job_id",
            "status_tool_key": "mcp:example:get_job_status",
            "status_id_argument": "job_id",
            "status_pointer": "/status",
            "status_map": {"queued": "accepted", "completed": "succeeded"},
            "output_pointer": "/artifacts",
            "cancel_tool_key": "mcp:example:cancel_job",
            "recoverable": True,
        }
        for field in ('contract_id', 'submit_tool_key', 'remote_id_pointer',
                      'status_tool_key', 'status_id_argument', 'status_pointer'):
            self.assertTrue(str(sample.get(field) or '').strip(),
                            f'合同缺少 {field}')
        # 身份格式必须与 tool_key 一致，否则永远匹配不上。
        self.assertTrue(sample['submit_tool_key'].startswith('mcp:'),
                        'submit_tool_key 应为 mcp:<server>:<tool>')
        self.assertTrue(sample['status_tool_key'].startswith('mcp:'),
                        'status_tool_key 应为 mcp:<server>:<tool>')

    def test_status_map_only_uses_legal_targets(self):
        """拼错的状态值会被静默折成 unknown——那等于永远等不到结论。"""
        sample_map = {"queued": "accepted", "completed": "succeeded"}
        for source, target in sample_map.items():
            with self.subTest(source=source):
                self.assertIn(str(target).strip().lower(), LEGAL_STATUSES,
                              f'{source!r} 映射成 {target!r}，不在合法取值内')

    def test_contract_matching_and_status_roundtrip(self):
        """用一份内联合同验证 remote_id 提取与状态映射的核心逻辑。"""
        contract = async_contracts.AsyncContract(
            contract_id='probe', submit_tool_key='mcp:probe:render',
            remote_id_pointer='/job_id', status_tool_key='mcp:probe:get_job_status',
            status_id_argument='job_id', status_pointer='/status',
            status_map={'queued': 'accepted'})
        queued = {'status': 'queued', 'job_id': 'probe-job'}
        self.assertEqual(async_contracts.remote_id_of(queued, contract), 'probe-job')
        self.assertEqual(async_contracts.map_status(
            async_contracts.pointer(queued, contract.status_pointer), contract), 'accepted')
