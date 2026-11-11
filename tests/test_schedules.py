"""定时任务：判定语义、认领与 fencing、派发、收敛与投递。

这里刻意覆盖的是**会在生产里出错、而且不容易复现**的那几处：
两个 Worker 同时 tick、旧副本拿着过期租约推进、上一轮没跑完、错过窗口、
重启后同一 occurrence 被重复派发。
"""
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from webpage_agent.core.config import settings
from webpage_agent.scheduler import decide as decide_module
from webpage_agent.scheduler import delivery as delivery_module
from webpage_agent.scheduler import tick as tick_module
from tests.exec_env import new_store, override_settings, reset_db


def make_row(**overrides):
    base = {
        'schedule_id': 'sch1', 'tenant_id': 'default', 'owner_user_id': 'default',
        'owner_role': 'member', 'platform_id': '', 'platform_subject': '',
        'name': '每日新闻', 'prompt': '整理今天的 AI 新闻', 'enabled': True,
        'schedule_type': 'recurring', 'rrule': 'FREQ=DAILY;BYHOUR=9;BYMINUTE=0',
        'scheduled_at': None, 'valid_from': None, 'valid_until': None,
        'next_run_at': time.time() - 60, 'next_attempt_at': 0.0,
        'last_run_at': None, 'last_status': '', 'last_error_code': '',
        'last_error_message': '', 'consecutive_failures': 0, 'deferred_count': 0,
        'connector_ids': [], 'delivery': {},
        'lease_owner': 'w1', 'lease_expires_at': time.time() + 60, 'lease_epoch': 1,
    }
    base.update(overrides)
    return base


class FakeHub:
    """只实现调度器真正用到的那部分 Hub。

    用假 Hub 是因为我们要测的是"调度决策"，不是 agent 执行；真的 `Hub.start`
    会去调模型，测试会既慢又不稳定。
    """

    def __init__(self, store, *, capacity: bool = False):
        self.store = store
        self.capacity = capacity
        self.calls: list[dict] = []
        self.counter = 0

    def at_capacity(self) -> bool:
        return self.capacity

    async def start(self, message, session_id=None, principal=None, **_kwargs):
        self.counter += 1
        task_id = f'task{self.counter}'
        run_id = f'run{self.counter}'
        await self.store.create_task(task_id, session_id, message)
        await self.store.ensure_task_meta(task_id, session_id=session_id, goal=message)
        await self.store.create_run(run_id, task_id, status='queued',
                                    tenant_id=getattr(principal, 'tenant_id', 'default'),
                                    owner_user_id=getattr(principal, 'user_id', 'default'))
        self.calls.append({'message': message, 'session_id': session_id,
                           'principal': principal, 'task_id': task_id,
                           'run_id': run_id})
        return SimpleNamespace(task_id=task_id, run_id=run_id, session_id=session_id)


class DecideTests(unittest.TestCase):
    def decide(self, **overrides):
        kwargs = dict(due=1000.0, now=1060.0, missed_window=86400.0, busy=False,
                      at_capacity=False, deferred_count=0, max_deferrals=5,
                      defer_backoff=60.0)
        kwargs.update(overrides)
        return decide_module.decide(**kwargs)

    def test_fires_when_due_and_idle(self):
        self.assertEqual(self.decide().action, decide_module.FIRE)

    def test_busy_defers_and_counts(self):
        decision = self.decide(busy=True)
        self.assertEqual(decision.action, decide_module.DEFER)
        self.assertTrue(decision.count_deferral)
        self.assertEqual(decision.next_attempt_at, 1120.0)

    def test_busy_stops_after_max_deferrals(self):
        decision = self.decide(busy=True, deferred_count=4, max_deferrals=5,
                               next_run_at=2000.0)
        self.assertEqual(decision.action, decide_module.SKIP)
        self.assertEqual(decision.code, 'deferred_too_long')
        self.assertEqual(decision.next_run_at, 2000.0)

    def test_capacity_defers_like_busy(self):
        self.assertEqual(self.decide(at_capacity=True).action, decide_module.DEFER)

    def test_missed_window_skips_without_catch_up(self):
        decision = self.decide(now=1000.0 + 3 * 86400)
        self.assertEqual(decision.action, decide_module.SKIP)
        self.assertEqual(decision.code, 'missed_window')

    def test_jitter_defers_without_counting(self):
        decision = self.decide(dispatch_at=1090.0)
        self.assertEqual(decision.action, decide_module.DEFER)
        self.assertFalse(decision.count_deferral)

    def test_jitter_delay_is_deterministic_across_processes(self):
        first = decide_module.jitter_delay('sch1', 1000.0, 300.0)
        second = decide_module.jitter_delay('sch1', 1000.0, 300.0)
        self.assertEqual(first, second)
        self.assertGreaterEqual(first, 0.0)
        self.assertLess(first, 300.0)
        self.assertEqual(decide_module.jitter_delay('sch1', 1000.0, 0.0), 0.0)


class ScheduleStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_db()
        self.store = new_store()

    async def asyncTearDown(self):
        await self.store.close()

    async def _create(self, **overrides):
        values = dict(schedule_id='sch1', tenant_id='t1', owner_user_id='u1',
                      name='每日新闻', prompt='跑一下', next_run_at=time.time() - 5,
                      rrule='FREQ=DAILY;BYHOUR=9;BYMINUTE=0')
        values.update(overrides)
        return await self.store.create_schedule(**values)

    async def test_create_and_ownership(self):
        await self._create()
        self.assertIsNotNone(await self.store.schedule('sch1', tenant_id='t1',
                                                       owner_user_id='u1'))
        self.assertIsNone(await self.store.schedule('sch1', tenant_id='t1',
                                                    owner_user_id='u2'))

    async def test_claim_is_exclusive_and_takeover_after_lease(self):
        await self._create()
        first = await self.store.claim_due_schedules(
            'w1', now_ts=time.time(), limit=10, lease_seconds=60)
        self.assertEqual([row['schedule_id'] for row in first], ['sch1'])
        second = await self.store.claim_due_schedules(
            'w2', now_ts=time.time(), limit=10, lease_seconds=60)
        self.assertEqual(second, [])
        # 租约过期后允许接手，且代次 +1（旧副本的写入会落空）
        later = time.time() + 120
        third = await self.store.claim_due_schedules(
            'w2', now_ts=later, limit=10, lease_seconds=60)
        self.assertEqual(len(third), 1)
        self.assertEqual(third[0]['lease_epoch'], first[0]['lease_epoch'] + 1)

    async def test_stale_lease_cannot_advance(self):
        await self._create()
        claimed = await self.store.claim_due_schedules(
            'w1', now_ts=time.time(), limit=10, lease_seconds=1)
        row = claimed[0]
        takeover = await self.store.claim_due_schedules(
            'w2', now_ts=time.time() + 5, limit=10, lease_seconds=60)
        self.assertEqual(len(takeover), 1)
        stale_ok = await self.store.finish_schedule_tick(
            'sch1', lease_owner=row['lease_owner'], lease_epoch=row['lease_epoch'],
            next_run_at=time.time() + 99999)
        self.assertFalse(stale_ok)
        fresh_ok = await self.store.finish_schedule_tick(
            'sch1', lease_owner=takeover[0]['lease_owner'],
            lease_epoch=takeover[0]['lease_epoch'], next_run_at=12345.0)
        self.assertTrue(fresh_ok)
        row_after = await self.store.schedule('sch1')
        self.assertEqual(row_after['next_run_at'], 12345.0)

    async def test_occurrence_is_unique_per_due(self):
        due = time.time()
        first = await self.store.record_occurrence(
            occurrence_id='occ1', schedule_id='sch1', due_at=due, status='running')
        second = await self.store.record_occurrence(
            occurrence_id='occ1', schedule_id='sch1', due_at=due, status='succeeded')
        self.assertEqual(first['occurrence_id'], second['occurrence_id'])
        rows = await self.store.list_occurrences('sch1')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'succeeded')

    async def test_disabled_schedule_is_not_claimed(self):
        await self._create(enabled=False)
        self.assertEqual(await self.store.claim_due_schedules(
            'w1', now_ts=time.time(), limit=10, lease_seconds=60), [])

    async def test_next_attempt_gate_blocks_early_reclaim(self):
        await self._create(next_attempt_at=time.time() + 300)
        self.assertEqual(await self.store.claim_due_schedules(
            'w1', now_ts=time.time(), limit=10, lease_seconds=60), [])

    async def test_list_sessions_hides_scheduled_runs(self):
        await self.store.create_schedule(
            schedule_id='sch1', tenant_id='t1', owner_user_id='u1',
            name='x', prompt='y', next_run_at=time.time())
        await self.store.append_message('bg1', 'user', 'hi', tenant_id='t1',
                                        owner_user_id='u1', dedupe_key='d1')
        await self.store.append_message('mine1', 'user', 'hello', tenant_id='t1',
                                        owner_user_id='u1', dedupe_key='d2')
        await self.store.record_occurrence(
            occurrence_id='occ1', schedule_id='sch1', tenant_id='t1',
            owner_user_id='u1', due_at=time.time(), status='running',
            session_id='bg1')
        listed = await self.store.list_sessions(tenant_id='t1', owner_user_id='u1')
        self.assertEqual([row['session_id'] for row in listed], ['mine1'])


class TickTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_db()
        self.store = new_store()
        self.hub = FakeHub(self.store)

    async def asyncTearDown(self):
        await self.store.close()

    async def _seed(self, **overrides):
        values = dict(schedule_id='sch1', tenant_id='t1', owner_user_id='u1',
                      name='每日新闻', prompt='整理今天的 AI 新闻',
                      next_run_at=time.time() - 30,
                      rrule='FREQ=DAILY;BYHOUR=9;BYMINUTE=0')
        values.update(overrides)
        return await self.store.create_schedule(**values)

    async def _tick(self, **setting_changes):
        changes = dict(scheduled_tasks_enabled=True, scheduler_tick_seconds=30.0,
                       scheduled_batch_limit=10, scheduled_claim_lease_seconds=120.0,
                       scheduled_max_deferrals=5, scheduled_defer_backoff_seconds=60.0,
                       scheduled_missed_window_seconds=86400.0,
                       scheduled_jitter_seconds=0.0)
        changes.update(setting_changes)
        with override_settings(**changes):
            return await tick_module.tick(self.hub, 'w1')

    async def test_disabled_by_default(self):
        await self._seed()
        stats = await tick_module.tick(self.hub, 'w1')
        self.assertEqual(stats['claimed'], 0)
        self.assertEqual(self.hub.calls, [])

    async def test_fires_and_advances(self):
        await self._seed()
        stats = await self._tick()
        self.assertEqual(stats['fired'], 1)
        self.assertEqual(len(self.hub.calls), 1)
        call = self.hub.calls[0]
        self.assertEqual(call['principal'].tenant_id, 't1')
        self.assertEqual(call['principal'].user_id, 'u1')
        row = await self.store.schedule('sch1')
        self.assertGreater(row['next_run_at'], time.time())
        self.assertEqual(row['last_status'], 'running')
        occurrences = await self.store.list_occurrences('sch1')
        self.assertEqual(occurrences[0]['status'], 'running')
        self.assertEqual(occurrences[0]['run_id'], 'run1')

    async def test_second_tick_does_not_double_fire(self):
        await self._seed()
        await self._tick()
        await self._tick()
        self.assertEqual(len(self.hub.calls), 1)

    async def test_busy_occurrence_defers_instead_of_stacking(self):
        due = time.time() - 30
        await self._seed(next_run_at=due)
        await self.store.record_occurrence(
            occurrence_id='occ-open', schedule_id='sch1', due_at=due - 3600,
            status='running', run_id='old-run')
        stats = await self._tick()
        self.assertEqual(stats['fired'], 0)
        self.assertEqual(stats['deferred'], 1)
        row = await self.store.schedule('sch1')
        self.assertGreater(row['next_attempt_at'], time.time())
        self.assertEqual(row['deferred_count'], 1)
        self.assertEqual(row['next_run_at'], due)

    async def test_missed_window_is_skipped_not_replayed(self):
        await self._seed(next_run_at=time.time() - 3 * 86400)
        stats = await self._tick()
        self.assertEqual(stats['skipped'], 1)
        self.assertEqual(self.hub.calls, [])
        occurrences = await self.store.list_occurrences('sch1')
        self.assertEqual(occurrences[0]['status'], 'skipped')
        self.assertEqual(occurrences[0]['error_code'], 'missed_window')
        row = await self.store.schedule('sch1')
        self.assertGreater(row['next_run_at'], time.time())

    async def test_once_schedule_fires_once_then_stops(self):
        due = time.time() - 5
        await self._seed(schedule_type='once', rrule='', next_run_at=due,
                         scheduled_at=datetime.fromtimestamp(due).isoformat())
        stats = await self._tick()
        self.assertEqual(stats['fired'], 1)
        row = await self.store.schedule('sch1')
        self.assertEqual(row['last_status'], 'running')
        # 第二次 tick：once 任务已无下一次，next_run_at 为空，不会再被认领
        await self._tick()
        self.assertEqual(len(self.hub.calls), 1)

    async def test_invalid_rrule_never_hot_loops(self):
        await self._seed(rrule='FREQ=DAILY;BYHOUR=99')
        stats = await self._tick()
        self.assertEqual(stats['fired'], 1)
        row = await self.store.schedule('sch1')
        self.assertIsNone(row['next_run_at'])
        await self._tick()
        self.assertEqual(len(self.hub.calls), 1)

    async def test_collect_finishes_run_and_enqueues_delivery(self):
        await self._seed(delivery={'channel': 'wecom',
                                   'webhook_url': 'https://example.com/hook',
                                   'events': ['success', 'failure', 'needs_action']})
        await self._tick()
        run_id = self.hub.calls[0]['run_id']
        await self.store.update_run(run_id, status='succeeded')
        task_id = self.hub.calls[0]['task_id']
        await self.store.update_task(task_id, status='succeeded',
                                     state={'status': 'succeeded',
                                            'answer': '今天有三条 AI 新闻'})
        with override_settings(scheduled_tasks_enabled=True):
            collected, delivered = await tick_module.collect(self.hub, time.time())
        self.assertEqual(collected, 1)
        self.assertEqual(delivered, 1)
        occurrences = await self.store.list_occurrences('sch1')
        self.assertEqual(occurrences[0]['status'], 'succeeded')
        self.assertIn('今天有三条 AI 新闻', occurrences[0]['summary'])
        # 任务行也要收成终态，否则列表卡片会永远显示"执行中"
        schedule = await self.store.schedule('sch1')
        self.assertEqual(schedule['last_status'], 'succeeded')
        self.assertEqual(schedule['consecutive_failures'], 0)
        self.assertEqual(occurrences[0]['delivery_status'], 'queued')
        job = await self.store.claim_job('w1')
        self.assertEqual(job['kind'], 'schedule_delivery')

    async def test_failed_run_counts_towards_consecutive_failures(self):
        await self._seed()
        await self._tick()
        run_id = self.hub.calls[0]['run_id']
        await self.store.update_run(run_id, status='failed', error='模型不可达')
        with override_settings(scheduled_tasks_enabled=True):
            await tick_module.collect(self.hub, time.time())
        schedule = await self.store.schedule('sch1')
        self.assertEqual(schedule['last_status'], 'failed')
        self.assertEqual(schedule['consecutive_failures'], 1)
        self.assertIn('不可达', schedule['last_error_message'])
        occurrences = await self.store.list_occurrences('sch1')
        self.assertIn('不可达', occurrences[0]['summary'])

    async def test_collect_marks_waiting_run_as_needs_action(self):
        await self._seed(delivery={'channel': 'webhook',
                                   'webhook_url': 'https://example.com/hook'})
        await self._tick()
        run_id = self.hub.calls[0]['run_id']
        await self.store.update_run(run_id, status='waiting_approval')
        with override_settings(scheduled_tasks_enabled=True):
            await tick_module.collect(self.hub, time.time())
        occurrences = await self.store.list_occurrences('sch1')
        self.assertEqual(occurrences[0]['status'], 'paused')
        self.assertEqual(occurrences[0]['error_code'], 'waiting_approval')
        # 暂停的 occurrence 不再占着"未结束"，下一次照常可以触发
        self.assertIsNone(await self.store.open_occurrence('sch1'))

    async def test_manual_run_does_not_move_next_run_at(self):
        await self._seed(next_run_at=time.time() + 3600)
        row_before = await self.store.schedule('sch1')
        occurrence = await tick_module.run_now(
            self.hub, 'sch1', 'manual:u1', tenant_id='t1', owner_user_id='u1')
        self.assertIsNotNone(occurrence)
        self.assertEqual(len(self.hub.calls), 1)
        row_after = await self.store.schedule('sch1')
        self.assertEqual(row_after['next_run_at'], row_before['next_run_at'])

    async def test_manual_run_is_refused_for_other_owner(self):
        await self._seed()
        occurrence = await tick_module.run_now(
            self.hub, 'sch1', 'manual:other', tenant_id='t1', owner_user_id='u2')
        self.assertIsNone(occurrence)
        self.assertEqual(self.hub.calls, [])


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_db()
        self.store = new_store()
        self.hub = FakeHub(self.store)
        await self.store.create_schedule(
            schedule_id='sch1', tenant_id='t1', owner_user_id='u1', name='每日新闻',
            prompt='x', next_run_at=time.time() - 5,
            delivery={'channel': 'wecom', 'webhook_url': 'https://example.com/hook'})
        await self.store.record_occurrence(
            occurrence_id='occ1', schedule_id='sch1', tenant_id='t1',
            owner_user_id='u1', due_at=time.time(), status='succeeded',
            summary='三条要点')

    async def asyncTearDown(self):
        await self.store.close()

    async def _job(self):
        await self.store.enqueue_job('job1', delivery_module.KIND, 'occ1:wecom',
                                     {'occurrence_id': 'occ1', 'schedule_id': 'sch1',
                                      'channel': 'wecom', 'notify': 'success',
                                      'status': 'succeeded', 'summary': '三条要点'})
        return await self.store.claim_job('w1')

    async def test_success_marks_sent(self):
        sent: list[tuple[str, dict]] = []

        async def fake_post(url, payload, *, channel='webhook', timeout=None,
                            transport=None):
            sent.append((channel, payload))
            return {'errcode': 0}

        job = await self._job()
        with patch.object(delivery_module.webhooks, 'post_webhook', fake_post):
            await delivery_module.deliver_job(self.hub, job)
        occurrence = await self.store.occurrence('occ1')
        self.assertEqual(occurrence['delivery_status'], 'sent')
        self.assertEqual(sent[0][0], 'wecom')
        self.assertIn('markdown', sent[0][1])

    async def test_retryable_failure_raises_and_records(self):
        async def failing(url, payload, *, channel='webhook', timeout=None,
                          transport=None):
            raise delivery_module.webhooks.DeliveryError('推送端返回 500', code='http_5xx')

        job = await self._job()
        with patch.object(delivery_module.webhooks, 'post_webhook', failing):
            with self.assertRaises(delivery_module.webhooks.DeliveryError):
                await delivery_module.deliver_job(self.hub, job)
        occurrence = await self.store.occurrence('occ1')
        self.assertEqual(occurrence['delivery_status'], 'failed')
        self.assertEqual(occurrence['delivery_attempts'], 1)

    async def test_permanent_failure_does_not_raise(self):
        async def rejected(url, payload, *, channel='webhook', timeout=None,
                           transport=None):
            raise delivery_module.webhooks.DeliveryError(
                '推送端拒绝', code='http_4xx', retryable=False)

        job = await self._job()
        with patch.object(delivery_module.webhooks, 'post_webhook', rejected):
            await delivery_module.deliver_job(self.hub, job)
        occurrence = await self.store.occurrence('occ1')
        self.assertEqual(occurrence['delivery_status'], 'failed')

    async def test_already_sent_is_skipped(self):
        await self.store.update_occurrence('occ1', delivery_status='sent')
        called: list[int] = []

        async def fake_post(*args, **kwargs):
            called.append(1)
            return {}

        job = await self._job()
        with patch.object(delivery_module.webhooks, 'post_webhook', fake_post):
            await delivery_module.deliver_job(self.hub, job)
        self.assertEqual(called, [])


class WebhookPayloadTests(unittest.TestCase):
    def test_validate_url(self):
        self.assertEqual(
            delivery_module.webhooks.validate_url('https://qyapi.weixin.qq.com/x'),
            'https://qyapi.weixin.qq.com/x')
        self.assertEqual(
            delivery_module.webhooks.validate_url('http://127.0.0.1:9000/hook'),
            'http://127.0.0.1:9000/hook')
        for bad in ('ftp://example.com', 'http://example.com/hook', '', 'https://'):
            with self.subTest(bad=bad), self.assertRaises(
                    delivery_module.webhooks.DeliveryError):
                delivery_module.webhooks.validate_url(bad)

    def test_channel_payloads(self):
        wecom = delivery_module.webhooks.build_payload(
            'wecom', name='日报', status_text='✅ 已完成', summary='三条')
        self.assertEqual(wecom['msgtype'], 'markdown')
        self.assertIn('日报', wecom['markdown']['content'])
        feishu = delivery_module.webhooks.build_payload(
            'feishu', name='日报', status_text='✅ 已完成', summary='三条')
        self.assertEqual(feishu['msg_type'], 'text')
        generic = delivery_module.webhooks.build_payload(
            'webhook', name='日报', status_text='ok', summary='三条', run_id='r1')
        self.assertEqual(generic['task'], '日报')
        self.assertEqual(generic['run_id'], 'r1')


class ImportGuardTests(unittest.TestCase):
    def test_default_switch_is_off(self):
        """总开关默认必须是关的：它是"无人值守执行 + 真花钱"的能力。"""
        self.assertFalse(settings.scheduled_tasks_enabled)
        self.assertGreaterEqual(settings.scheduler_tick_seconds, 5.0)


if __name__ == '__main__':
    unittest.main()
