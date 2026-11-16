"""给「机器人（Bot Mode）」灌一份可演示的种子数据。

为什么需要它：新库打开 `/api/ui/bots` 是空的，花名册、分区、群聊、例行任务
四个界面都要手工点一遍才能看出形态。这个脚本一次建好一组**产品上有意义的**
机器人（代码审查 / 写手 / 研究员），并把它们放进同一个分区与一个群聊里。

幂等：按 `handle` 判重，重复跑只会补齐缺的那几个，不会建出两套同名机器人。
不写任何假对话——聊天记录要靠真实模型跑出来，预置的假消息只会让人误判
"它到底能不能干活"。

用法：

    python -m scripts.seed_demo_bots                  # 给 default 账号建
    python -m scripts.seed_demo_bots --tenant t1 --user u1
    python -m scripts.seed_demo_bots --room           # 额外建一个群聊房间
    python -m scripts.seed_demo_bots --routine        # 额外建一条每日 9 点日报

注意：真正聊天需要 `LLM_KEY`（或等价的网关密钥）；本脚本只建身份与会话。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timezone

from webpage_agent.bot import prompts
from webpage_agent.core.config import settings
from webpage_agent.store.pg_store import PostgresStore

BOTS = (
    {
        'handle': 'reviewer', 'name': '代码审查员', 'icon': '🛡️', 'color': '#eef4ff',
        'title': '只报会出事的代码问题',
        'description': '上线前把关：并发、越权、数据丢失；不评论风格。',
        'soul': ('你是资深代码审查员。只报会导致线上事故的问题，按严重程度排序，'
                 '每条给出可执行的改法。不确定就说不确定；不要为了凑数报风格问题。'),
        'pinned': True,
    },
    {
        'handle': 'writer', 'name': '写手', 'icon': '✍️', 'color': '#fff3f0',
        'title': '把技术结论写成人话',
        'description': '给业务看的说明、发版公告、事故复盘。',
        'soul': '你只写短句，不用术语，先给结论，再给依据。一次最多写三句。',
    },
    {
        'handle': 'research', 'name': '研究员', 'icon': '🔍', 'color': '#eefaf3',
        'title': '每天 9 点 AI 热点日报',
        'description': '检索、交叉验证、给出来源；有矛盾必须标出来。',
        'soul': ('你只引用可核对的来源，每条结论后面跟来源链接。'
                 '互相矛盾的说法要单列一节，不要替读者下结论。'),
    },
)

SECTION_NAME = '中台项目'
ROOM_NAME = '发版评审'
ROOM_TOPIC = '@reviewer 周五发版，还有哪些阻塞项？@writer 发版说明写好了吗？'
ROUTINE_NAME = 'X AI 热点日报'
ROUTINE_PROMPT = ('检索今天 AI 领域最重要的 5 条动态，每条给出来源链接与一句影响判断；'
                  '有互相矛盾的说法单独列出来。')


async def seed(tenant: str, user: str, *, room: bool, routine: bool) -> int:
    store = PostgresStore(settings.database_url)
    try:
        await store.ready()
        existing = {b['handle']: b for b in await store.list_bot_rows(
            tenant_id=tenant, owner_user_id=user, include_archived=True)}

        section = next((row for row in await store.list_bot_sections(
            tenant_id=tenant, owner_user_id=user) if row['name'] == SECTION_NAME), None)
        if section is None:
            section = await store.upsert_bot_section(
                section_id='sec_' + uuid.uuid4().hex[:10], tenant_id=tenant,
                owner_user_id=user, name=SECTION_NAME, position=0)
            print(f'[seed] 新建分区 {SECTION_NAME}')

        created = []
        for spec in BOTS:
            if spec['handle'] in existing:
                continue
            stamp = datetime.now(timezone.utc).isoformat()
            bot = await store.create_bot_row(
                bot_id='bot_' + uuid.uuid4().hex[:12], tenant_id=tenant,
                owner_user_id=user, created_at=stamp, updated_at=stamp,
                status='active', section_id=section['section_id'], **spec)
            await store.ensure_bot_session(
                bot_id=bot['bot_id'], tenant_id=tenant, owner_user_id=user,
                session_kind='bot_chat', title=prompts.BOT_CHAT_TITLE)
            created.append(bot)
            print(f"[seed] 新建机器人 {spec['name']} (@{spec['handle']})")

        bots = {b['handle']: b for b in await store.list_bot_rows(
            tenant_id=tenant, owner_user_id=user)}
        if not bots:
            print('[seed] 没有可用的机器人，退出', file=sys.stderr)
            return 1

        if room:
            rooms = await store.list_room_rows(tenant_id=tenant, owner_user_id=user)
            if not any(row['name'] == ROOM_NAME for row in rooms):
                members = [row['bot_id'] for handle, row in bots.items()
                           if handle in ('reviewer', 'writer')]
                if len(members) >= 2:
                    created_room = await store.create_room_row(
                        room_id='room_' + uuid.uuid4().hex[:12], tenant_id=tenant,
                        owner_user_id=user, name=ROOM_NAME, members=members,
                        topic=ROOM_TOPIC,
                        max_rounds=settings.bot_room_max_rounds,
                        max_messages=settings.bot_room_max_messages)
                    room_id = created_room['room_id']
                    await store.append_room_event(
                        room_id, event_id=f'{room_id}:opened', kind='room.opened',
                        actor=f'user:{user}',
                        payload={'name': ROOM_NAME, 'topic': ROOM_TOPIC})
                    await store.append_room_event(
                        room_id, event_id=f'{room_id}:topic', kind='message.user',
                        actor=f'user:{user}',
                        payload={'text': ROOM_TOPIC, 'epoch': 1,
                                 'mentions': prompts.parse_mentions(ROOM_TOPIC)})
                    print(f'[seed] 新建群聊 {ROOM_NAME}（{len(members)} 个成员）')

        if routine and 'research' in bots:
            rows = await store.list_schedules(tenant_id=tenant, owner_user_id=user,
                                              limit=200)
            if not any(row['name'] == ROUTINE_NAME for row in rows):
                import time

                from webpage_agent.core import rrules
                rule = rrules.parse('FREQ=DAILY;BYHOUR=9;BYMINUTE=0')
                from datetime import datetime as _dt
                nxt = rrules.next_run_at(rule, time.time())
                await store.create_schedule(
                    schedule_id=uuid.uuid4().hex[:12], tenant_id=tenant,
                    owner_user_id=user, owner_role='member',
                    name=ROUTINE_NAME, prompt=ROUTINE_PROMPT,
                    enabled=bool(settings.scheduled_tasks_enabled),
                    schedule_type='recurring',
                    rrule='FREQ=DAILY;BYHOUR=9;BYMINUTE=0',
                    next_run_at=nxt, connector_ids=[],
                    bot_id=bots['research']['bot_id'])
                when = (_dt.fromtimestamp(nxt).strftime('%Y-%m-%d %H:%M')
                        if nxt else '（规则未生效）')
                print(f'[seed] 新建例行任务 {ROUTINE_NAME}，下次 {when}')

        print('\n打开 http://127.0.0.1:8000/api/ui/bots 查看（端口按实际部署改）')
        print('提示：真正聊天需要 LLM_KEY；定时任务还需要 SCHEDULED_TASKS_ENABLED=true。')
        return 0
    finally:
        await store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description='机器人（Bot Mode）演示数据')
    parser.add_argument('--tenant', default='default')
    parser.add_argument('--user', default='default')
    parser.add_argument('--room', action='store_true', help='同时建一个群聊')
    parser.add_argument('--routine', action='store_true', help='同时建一条每日日报')
    parser.add_argument('--all', action='store_true', help='群聊 + 例行任务都建')
    args = parser.parse_args()
    code = asyncio.run(seed(args.tenant, args.user,
                            room=args.room or args.all,
                            routine=args.routine or args.all))
    raise SystemExit(code)


if __name__ == '__main__':
    main()
