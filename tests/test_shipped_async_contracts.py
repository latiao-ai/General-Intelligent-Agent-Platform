"""出厂 `contracts/async_tools.json` 必须真的能加载、且字段合法。

`async_contracts.load()` 遇到 JSON 错误或非法行会**静默返回空清单**，
`summary()` 于是报 0，启动日志退回"没有可恢复合同"的警告——异步长任务
退化成"已排队"被当成成功。这条链路上没有任何异常，只有一条日志，
所以必须用测试把出厂文件钉住。
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

    def test_shipped_file_yields_recoverable_contracts(self):
        """把出厂文件当成运行时输入解析，必须得到可恢复合同。"""
        parsed = [row for row in self.rows
                  if row.get('remote_id_pointer') and row.get('status_tool_key')
                  and row.get('status_id_argument')]
        self.assertTrue(parsed, '出厂合同里没有任何一份满足可恢复条件')
        for row in parsed:
            self.assertTrue(row.get('recoverable', True),
                            f"{row.get('contract_id')} 显式标成不可恢复")

    def test_every_contract_has_required_pointers_and_keys(self):
        for row in self.rows:
            with self.subTest(contract=row.get('contract_id')):
                self.assertTrue(str(row.get('contract_id') or '').strip(),
                                '合同缺少 contract_id')
                for field in ('submit_tool_key', 'remote_id_pointer',
                              'status_tool_key', 'status_id_argument', 'status_pointer'):
                    self.assertTrue(str(row.get(field) or '').strip(),
                                    f'{row.get("contract_id")} 缺少 {field}')
                # 身份格式必须与 tool_key 一致，否则永远匹配不上。
                self.assertTrue(row['submit_tool_key'].startswith('mcp:'),
                                'submit_tool_key 应为 mcp:<server>:<tool>')
                self.assertTrue(row['status_tool_key'].startswith('mcp:'),
                                'status_tool_key 应为 mcp:<server>:<tool>')

    def test_status_map_only_uses_legal_targets(self):
        """拼错的状态值会被静默折成 unknown——那等于永远等不到结论。"""
        for row in self.rows:
            for source, target in (row.get('status_map') or {}).items():
                with self.subTest(contract=row.get('contract_id'), source=source):
                    self.assertIn(str(target).strip().lower(), LEGAL_STATUSES,
                                  f'{row.get("contract_id")} 把 {source!r} 映射成 '
                                  f'{target!r}，不在合法取值内，会被静默折成 unknown')

    def test_shipped_contracts_match_a_queued_receipt(self):
        """用服务端真实回执形状验证出厂合同能认出"已受理"。"""
        queued = {'status': 'queued', 'job_id': 'probe-job',
                  'tool_name': 'video_compose', 'next_action': 'poll_get_job_status'}
        matched = [row['submit_tool_key'] for row in self.rows
                   if row.get('submit_tool_key') == 'mcp:openmontage:render_video']
        self.assertTrue(matched, 'render_video 的合同不见了')
        contract = async_contracts.AsyncContract(
            contract_id='probe', submit_tool_key=matched[0],
            remote_id_pointer='/job_id', status_tool_key='mcp:openmontage:get_job_status',
            status_id_argument='job_id', status_pointer='/status',
            status_map={'queued': 'accepted'})
        self.assertEqual(async_contracts.remote_id_of(queued, contract), 'probe-job')
        self.assertEqual(async_contracts.map_status(
            async_contracts.pointer(queued, contract.status_pointer), contract), 'accepted')
