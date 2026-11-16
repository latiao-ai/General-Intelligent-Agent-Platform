"""Bot 广场（docs/bot-plaza.md）的测试。

分两层：

1. **纯函数**：分类白名单、标签清洗、可见性、两种视图字段。不碰库——
   这些错了不会报错，只会让"广场上看得见但点进去 404"这类矛盾悄悄出现。
2. **接口**：投稿 → 审核 → 启用（幂等）→ 卸载，跨租户隔离，控制台准入。
   需要 `PG_TEST_DSN`，没有就整类跳过。

接口用例里控制台身份用 `dependency_overrides` 注入而不是伪造 header：
`platform_admin` 是准入判断的输入，伪造 header 会绕过被测的那段逻辑。
"""
import unittest
import uuid

from webpage_agent.bot import plaza as domain
from tests.exec_env import PG_DSN, new_store


# ---------------------------------------------------------------- 纯函数


class CategoryTests(unittest.TestCase):

    def test_known_category_passes_through(self):
        self.assertEqual(domain.normalize_category('coding'), 'coding')

    def test_unknown_falls_back_to_general(self):
        for value in ('', ' ', 'nope', None, '通用'):
            self.assertEqual(domain.normalize_category(value), 'general')


class TagTests(unittest.TestCase):

    def test_dedupes_and_trims(self):
        self.assertEqual(domain.clean_tags([' 并发 ', '并发', '上线']), ['并发', '上线'])

    def test_limits(self):
        self.assertEqual(len(domain.clean_tags([f't{i}' for i in range(20)])), domain.MAX_TAGS)

    def test_drops_empty(self):
        self.assertEqual(domain.clean_tags(['', None, 'x']), ['x'])

    def test_starters_limit(self):
        self.assertEqual(len(domain.clean_starters([f's{i}' for i in range(20)])),
                         domain.MAX_STARTERS)


class VisibilityTests(unittest.TestCase):

    def test_only_published_is_visible(self):
        base = {'status': 'published', 'scope': 'public'}
        self.assertTrue(domain.visible_to(base, 't1'))
        for status in ('draft', 'pending', 'rejected', 'offline'):
            self.assertFalse(domain.visible_to({**base, 'status': status}, 't1'), status)

    def test_tenant_scope_only_for_publisher_tenant(self):
        item = {'status': 'published', 'scope': 'tenant', 'publisher_tenant': 't1'}
        self.assertTrue(domain.visible_to(item, 't1'))
        self.assertFalse(domain.visible_to(item, 't2'))

    def test_public_crosses_tenant(self):
        item = {'status': 'published', 'scope': 'public', 'publisher_tenant': 't1'}
        self.assertTrue(domain.visible_to(item, 't2'))


class ViewTests(unittest.TestCase):

    def _item(self):
        return {'item_id': 'plz_1', 'name': '审查员', 'handle': 'reviewer',
                'title': '挑会出事的毛病', 'description': '…', 'soul': '你是审查员…',
                'readme': '长文', 'starters': ['审一下'], 'tags': ['代码'],
                'category': 'coding', 'status': 'published', 'scope': 'public',
                'install_count': 3, 'featured': 1, 'version': 2,
                'reviewed_at': '2026-11-01T00:00:00+00:00'}

    def test_list_view_hides_heavy_fields(self):
        view = domain.list_view(self._item())
        self.assertNotIn('soul', view)
        self.assertNotIn('readme', view)
        self.assertNotIn('starters', view)
        self.assertTrue(view['featured'])
        self.assertEqual(view['install_count'], 3)
        self.assertEqual(view['published_at'], '2026-11-01T00:00:00+00:00')

    def test_detail_view_has_persona(self):
        view = domain.detail_view(self._item(), installed_bot_id='bot_9')
        self.assertEqual(view['soul'], '你是审查员…')
        self.assertEqual(view['starters'], ['审一下'])
        self.assertTrue(view['installed'])
        self.assertEqual(view['installed_bot_id'], 'bot_9')

    def test_installed_flag_follows_bot_id(self):
        self.assertFalse(domain.list_view(self._item())['installed'])


# ---------------------------------------------------------------- 接口


@unittest.skipUnless(PG_DSN, '需要 PG_TEST_DSN')
class PlazaApiTests(unittest.IsolatedAsyncioTestCase):
    """从投稿到启用的整条链路。

    身份用依赖覆盖注入，而不是伪造请求头：`platform_admin` 是准入判断的**输入**，
    伪造 header 会把被测的那段逻辑一起绕过去。每一步动作前都显式切一次身份——
    依赖覆盖是全局的，忘记切会得到"管理员去操作别人机器人"这种假失败。
    """

    async def asyncSetUp(self):
        import httpx
        import webpage_agent.api.routes as api_module
        from webpage_agent.api.api_hub import Hub

        self.store = new_store()
        # 广场条目是跨租户共享的（public 对所有人可见），所以每个用例先清空这两张表：
        # 不清的话上一个用例的 public 条目会漏进"列表为空"的断言里。
        async with self.store._lease() as conn:
            await conn.execute('TRUNCATE TABLE bot_plaza_installs, bot_plaza_items')
        self.api = api_module
        self.policies = __import__(
            'webpage_agent.governance.policies', fromlist=['current_principal'])
        self.saved_store = getattr(api_module.app.state, 'store', None)
        self.saved_hub = api_module.hub
        api_module.app.state.store = self.store
        api_module.hub = Hub(self.store)
        suffix = uuid.uuid4().hex[:8]
        self.tenant = f't_{suffix}'
        self.user = f'u_{suffix}'
        self.admin = 'u_admin_' + suffix
        self.other_tenant = f'{self.tenant}_other'
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.api.app),
            base_url='http://test')
        self._principal = None
        self._as(self.user, 'member')

    async def asyncTearDown(self):
        self.api.app.dependency_overrides.pop(self.policies.current_principal, None)
        await self.client.aclose()
        await self.api.hub.close()
        self.api.hub = self.saved_hub
        self.api.app.state.store = self.saved_store
        await self.store.close()

    # ---- 工具 ----

    def _as(self, user, role='member'):
        from webpage_agent.governance.policies import Principal
        principal = Principal(tenant_id=self.tenant, user_id=user, role=role,
                              authenticated=True)
        self._principal = principal
        self.api.app.dependency_overrides[self.policies.current_principal] = \
            lambda: principal
        return principal

    async def _bot(self, name='审查员', handle='reviewer', **extra):
        res = await self.client.post('/api/bots', json={
            'name': name, 'handle': handle, 'title': '挑会出事的毛病',
            'soul': '你是资深代码审查员，只报会导致线上事故的问题。',
            'skill_ids': ['code-review'], **extra})
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()

    async def _publish(self, bot_id, **extra):
        payload = {'category': 'coding', 'tags': ['代码', '上线'],
                   'readme': '上线前过一遍。', 'starters': ['审一下这个仓库'], **extra}
        res = await self.client.post(f'/api/bots/{bot_id}/publish', json=payload)
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()

    async def _approve(self, item_id, **extra):
        self._as(self.admin, 'platform_admin')
        payload = {'status': 'published', 'review_note': '审核通过', **extra}
        res = await self.client.patch(f'/api/console/plaza/{item_id}', json=payload)
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    # ---- 投稿与审核 ----

    async def test_publish_waits_for_review_and_is_not_listed(self):
        bot = await self._bot()
        submitted = await self._publish(bot['bot_id'])
        self.assertEqual(submitted['status'], 'pending')
        self.assertEqual(submitted['scope'], 'tenant')

        plaza = await self.client.get('/api/bot-plaza')
        self.assertEqual(plaza.status_code, 200, plaza.text)
        self.assertEqual(plaza.json()['items'], [])

        mine = await self.client.get('/api/bot-plaza/mine')
        items = mine.json()['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['source_bot_id'], bot['bot_id'])
        self.assertEqual(items[0]['status'], 'pending')

    async def test_approve_makes_it_visible_to_tenant(self):
        bot = await self._bot()
        submitted = await self._publish(bot['bot_id'])
        await self._approve(submitted['item_id'])

        self._as(self.user, 'member')
        plaza = await self.client.get('/api/bot-plaza')
        items = plaza.json()['items']
        self.assertEqual([row['item_id'] for row in items], [submitted['item_id']])
        self.assertEqual(items[0]['category'], 'coding')
        self.assertNotIn('soul', items[0])

        detail = await self.client.get(f'/api/bot-plaza/{submitted["item_id"]}')
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()['starters'], ['审一下这个仓库'])

    async def test_republish_bumps_version_and_reenters_review(self):
        bot = await self._bot()
        first = await self._publish(bot['bot_id'])
        await self._approve(first['item_id'])

        self._as(self.user, 'member')
        second = await self._publish(bot['bot_id'], readme='补了详细介绍')
        self.assertEqual(second['item_id'], first['item_id'])
        self.assertEqual(second['version'], 2)
        self.assertEqual(second['status'], 'pending')
        item = await self.store.plaza_item(first['item_id'])
        self.assertEqual(item['readme'], '补了详细介绍')

    async def test_rejected_item_keeps_reason(self):
        bot = await self._bot()
        submitted = await self._publish(bot['bot_id'])
        self._as(self.admin, 'platform_admin')
        res = await self.client.patch(
            f"/api/console/plaza/{submitted['item_id']}",
            json={'status': 'rejected', 'review_note': '先写清适用场景'})
        self.assertEqual(res.status_code, 200, res.text)

        self._as(self.user, 'member')
        mine = await self.client.get('/api/bot-plaza/mine')
        self.assertEqual(mine.json()['items'][0]['review_note'], '先写清适用场景')
        plaza = await self.client.get('/api/bot-plaza')
        self.assertEqual(plaza.json()['items'], [])

    async def test_withdraw_offline(self):
        bot = await self._bot()
        submitted = await self._publish(bot['bot_id'])
        res = await self.client.delete(f'/api/bot-plaza/mine/{submitted["item_id"]}')
        self.assertEqual(res.status_code, 200, res.text)
        item = await self.store.plaza_item(submitted['item_id'])
        self.assertEqual(item['status'], 'offline')

    # ---- 启用 / 卸载 ----

    async def test_install_copies_persona_and_is_idempotent(self):
        bot = await self._bot()
        submitted = await self._publish(bot['bot_id'])
        await self._approve(submitted['item_id'])

        self._as(self.user, 'member')
        first = await self.client.post(
            f'/api/bot-plaza/{submitted["item_id"]}/install')
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        self.assertTrue(body['installed'])
        self.assertTrue(body['bot_id'].startswith('bot_'))
        self.assertIn('?bot=', body['chat_url'])

        copied = await self.store.bot_row(body['bot_id'], tenant_id=self.tenant,
                                          owner_user_id=self.user)
        self.assertEqual(copied['soul'],
                         '你是资深代码审查员，只报会导致线上事故的问题。')
        self.assertEqual(copied['skill_ids'], ['code-review'])
        session = await self.store.bot_session(body['bot_id'])
        self.assertTrue(session['session_id'])

        again = await self.client.post(f'/api/bot-plaza/{submitted["item_id"]}/install')
        self.assertEqual(again.status_code, 200, again.text)
        self.assertFalse(again.json()['installed'])
        self.assertEqual(again.json()['bot_id'], body['bot_id'])

        roster = await self.client.get('/api/bots')
        self.assertEqual(len(roster.json()['items']), 2)   # 原机器人 + 副本
        item = await self.store.plaza_item(submitted['item_id'])
        self.assertEqual(item['install_count'], 1)

    async def test_installer_sees_installed_flag(self):
        bot = await self._bot()
        submitted = await self._publish(bot['bot_id'])
        await self._approve(submitted['item_id'])
        self._as(self.user, 'member')
        installed = (await self.client.post(
            f'/api/bot-plaza/{submitted["item_id"]}/install')).json()

        plaza = await self.client.get('/api/bot-plaza')
        row = plaza.json()['items'][0]
        self.assertTrue(row['installed'])
        self.assertEqual(row['installed_bot_id'], installed['bot_id'])

    async def test_uninstall_archives_copy(self):
        bot = await self._bot()
        submitted = await self._publish(bot['bot_id'])
        await self._approve(submitted['item_id'])
        self._as(self.user, 'member')
        installed = (await self.client.post(
            f'/api/bot-plaza/{submitted["item_id"]}/install')).json()

        res = await self.client.delete(f'/api/bot-plaza/{submitted["item_id"]}/install')
        self.assertEqual(res.status_code, 200, res.text)
        row = await self.store.bot_row(installed['bot_id'], tenant_id=self.tenant,
                                       owner_user_id=self.user)
        self.assertEqual(row['status'], 'archived')
        install = await self.store.plaza_install_for(
            item_id=submitted['item_id'], tenant_id=self.tenant,
            owner_user_id=self.user)
        self.assertIsNone(install)
        # 归档后不再占"已装"位：再点「使用」会重建一份新副本
        again = (await self.client.post(
            f'/api/bot-plaza/{submitted["item_id"]}/install')).json()
        self.assertTrue(again['installed'])
        self.assertNotEqual(again['bot_id'], installed['bot_id'])

    async def test_pending_item_cannot_be_installed(self):
        bot = await self._bot()
        submitted = await self._publish(bot['bot_id'])
        res = await self.client.post(f'/api/bot-plaza/{submitted["item_id"]}/install')
        self.assertEqual(res.status_code, 404, res.text)

    # ---- 隔离与准入 ----

    async def test_tenant_scoped_item_hidden_from_other_tenant(self):
        from webpage_agent.governance.policies import Principal
        bot = await self._bot()
        submitted = await self._publish(bot['bot_id'])
        await self._approve(submitted['item_id'])

        # 直接换一个租户的主体，而不是改 header：依赖覆盖优先于 header。
        other = Principal(tenant_id=self.other_tenant, user_id='u_other',
                          role='member', authenticated=True)
        self.api.app.dependency_overrides[self.policies.current_principal] = lambda: other
        plaza = await self.client.get('/api/bot-plaza')
        self.assertEqual(plaza.json()['items'], [])
        detail = await self.client.get(f'/api/bot-plaza/{submitted["item_id"]}')
        self.assertEqual(detail.status_code, 404)

    async def test_public_item_crosses_tenant(self):
        from webpage_agent.governance.policies import Principal
        self._as(self.admin, 'platform_admin')
        res = await self.client.post('/api/console/plaza', json={
            'name': '公开助手', 'handle': 'public-bot', 'title': '谁都能用',
            'soul': '你是通用助手。', 'category': 'general', 'scope': 'public',
            'status': 'published', 'readme': '对所有人开放。'})
        self.assertEqual(res.status_code, 200, res.text)
        item_id = res.json()['item_id']

        other = Principal(tenant_id=self.other_tenant, user_id='u_other',
                          role='member', authenticated=True)
        self.api.app.dependency_overrides[self.policies.current_principal] = lambda: other
        plaza = await self.client.get('/api/bot-plaza')
        self.assertEqual([row['item_id'] for row in plaza.json()['items']], [item_id])

    async def test_member_cannot_open_console_plaza(self):
        self._as(self.user, 'member')
        res = await self.client.get('/api/console/plaza')
        self.assertEqual(res.status_code, 403, res.text)

    async def test_operator_cannot_publish_cross_tenant(self):
        bot = await self._bot()
        submitted = await self._publish(bot['bot_id'])
        self._as('u_op', 'operator')
        res = await self.client.patch(f"/api/console/plaza/{submitted['item_id']}",
                                      json={'scope': 'public'})
        self.assertEqual(res.status_code, 403, res.text)

    async def test_publish_others_bot_is_404(self):
        bot = await self._bot()
        self._as('u_someone', 'member')
        res = await self.client.post(f"/api/bots/{bot['bot_id']}/publish",
                                     json={'category': 'coding'})
        self.assertEqual(res.status_code, 404)


if __name__ == '__main__':
    unittest.main()
