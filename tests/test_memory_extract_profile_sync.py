"""自动提取与画像的边界。

注入时画像是优先的那一份（见 `render_long_term(override_keys=...)`），
所以"画像有称呼、用户改口"必须回写画像，否则改口等于没改。
反过来，画像不能变成第二份记忆：只有身份型的键允许新增，总量还有上限。
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from webpage_agent.core import memory, profile
from webpage_agent.core.config import settings
from webpage_agent.core.services import (
    AttemptTimeout, ModelRequestError, StructuredOutputError)


class _Store:
    def __init__(self):
        self.rows = []

    async def remember(self, **kwargs):
        self.rows.append(kwargs)
        return 'mem_fake'


class ExtractProfileSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.saved = settings.profile_path
        object.__setattr__(settings, 'profile_path',
                           Path(self._tmp.name) / 'USER.md')
        profile.merge({'称呼': '浩南', '偏好': '先给结论'})

    async def asyncTearDown(self):
        object.__setattr__(settings, 'profile_path', self.saved)
        self._tmp.cleanup()

    async def extract(self, facts):
        result = SimpleNamespace(model_dump=lambda: {'facts': facts})
        store = _Store()
        with patch.object(memory, 'structured_call',
                          AsyncMock(return_value=(result, {'input_tokens': 200,
                                                           'output_tokens': 40}))):
            outcome = await memory.extract_user_memory(
                store, scope_id='default', tenant_id='tenant-a',
                user_message='以后叫我南哥', answer='好')
        return outcome, store

    async def test_existing_profile_key_is_rewritten(self):
        outcome, store = await self.extract({'称呼': '南哥'})
        self.assertEqual(outcome.saved, 1)
        self.assertEqual(outcome.learned, {'称呼': '南哥'})   # 只有变化量才配播报
        self.assertEqual(profile.load()['称呼'], '南哥')
        self.assertEqual(profile.load()['偏好'], '先给结论')   # 其他键不动
        self.assertEqual(store.rows[0]['predicate'], '称呼')
        self.assertEqual(store.rows[0]['scope_type'], 'user')

    async def test_unknown_key_does_not_grow_profile(self):
        await self.extract({'本次议题': '下周的发布计划'})
        self.assertNotIn('本次议题', profile.load())
        self.assertEqual(len(profile.load()), 2)          # 只有 setUp 那两条

    async def test_identity_key_bootstraps_empty_profile(self):
        """空画像必须能自己长出来，否则面板永远显示「尚未建立」。"""
        profile.clear()
        self.assertEqual(profile.load(), {})
        await self.extract({'称呼': '南哥', '城市': '深圳'})
        self.assertEqual(profile.load(), {'称呼': '南哥', '城市': '深圳'})

    async def test_profile_stops_growing_at_cap(self):
        """上限到了就只改不增：档案不许被自动提取撑成第二份记忆。"""
        full = {f'偏好{i}': f'值{i}' for i in range(memory.PROFILE_MAX_KEYS)}
        profile.save(full)
        await self.extract({'城市': '深圳'})
        self.assertNotIn('城市', profile.load())
        self.assertEqual(len(profile.load()), memory.PROFILE_MAX_KEYS)

    async def test_place_words_are_identity_keys(self):
        """"我来自浙江台州"抽出来的键多半是「籍贯」而不是「城市」。"""
        await self.extract({'籍贯': '浙江台州'})
        self.assertEqual(profile.load()['籍贯'], '浙江台州')

    async def test_empty_extraction_is_noop(self):
        outcome, store = await self.extract({})
        self.assertEqual(outcome.saved, 0)
        self.assertEqual(outcome.learned, {})
        self.assertEqual(store.rows, [])
        self.assertEqual(profile.load()['称呼'], '浩南')

    async def test_repeating_known_fact_learns_nothing(self):
        """模型把画像里已有的事实再抄一遍时，learned 必须为空。

        它是界面「已记住」的唯一依据——复述一遍就弹一次，用户会觉得系统在瞎记。
        """
        outcome, _ = await self.extract({'称呼': '浩南'})   # setUp 里就是浩南
        self.assertEqual(outcome.learned, {})

    async def test_usage_is_returned_for_cost_accounting(self):
        outcome, _ = await self.extract({'城市': '台州'})
        self.assertEqual(outcome.usage['input_tokens'], 200)
        self.assertEqual(outcome.usage['output_tokens'], 40)

    # ---------- 失败回执：抽不成也要说一声 ----------

    async def extract_with_failure(self, exc):
        store = _Store()
        with patch.object(memory, 'structured_call', AsyncMock(side_effect=exc)):
            outcome = await memory.extract_user_memory(
                store, scope_id='default', tenant_id='tenant-a',
                user_message='我来自浙江台州',
                answer='已记录：唐浩南，来自浙江台州。')
        return outcome, store

    async def test_failure_reports_reason_instead_of_silence(self):
        """模型不可用时必须带回原因。

        以前这里 `except: return ExtractResult()`，界面上一个字都没有，而模型的
        回答里已经写着"已记录"——用户只能自己打开画像发现对不上账。
        """
        outcome, store = await self.extract_with_failure(ModelRequestError())
        self.assertEqual(outcome.error, '记忆抽取调用失败')
        self.assertEqual(outcome.learned, {})
        self.assertEqual(outcome.saved, 0)
        self.assertEqual(store.rows, [])
        self.assertEqual(profile.load()['称呼'], '浩南')      # 画像不许被一次失败改坏

    async def test_timeout_is_told_apart_from_other_failures(self):
        """超时下一轮多半能自愈，说成"失败"会让人去查本来没坏的配置。"""
        outcome, _ = await self.extract_with_failure(AttemptTimeout(12))
        self.assertEqual(outcome.error, '记忆抽取超时')

    async def test_structurally_invalid_output_is_told_apart(self):
        outcome, _ = await self.extract_with_failure(StructuredOutputError())
        self.assertEqual(outcome.error, '记忆抽取返回的结构不合法')

    async def test_empty_extraction_is_not_a_failure(self):
        """这轮本来就没说什么值得记的，不能报成失败——否则每句寒暄都弹警告。"""
        outcome, _ = await self.extract({})
        self.assertEqual(outcome.error, '')


if __name__ == '__main__':                                  # pragma: no cover
    unittest.main()
