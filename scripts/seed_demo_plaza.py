"""给「Bot 广场」灌一份可演示的官方条目。

为什么需要它：新库打开 `/api/ui/plaza` 是空的，分类条、卡片、详情抽屉、一键启用
四个界面都要手工造数据才看得出形态。这个脚本一次建好几条**产品上有意义的**
平台 Bot（公开可见、已上架），用户点「使用」就会在自己名下拿到副本。

幂等：按 `slug` 判重，重复跑只补缺的那几条，不会建出两套同名的。
不写任何假安装量——安装数只该来自真实的启用行为，写死一个"128 人用过"会让
运营判断失真。

用法：

    python -m scripts.seed_demo_plaza                 # 公开可见的 4 条
    python -m scripts.seed_demo_plaza --scope tenant   # 只在本企业可见
"""
from __future__ import annotations

import argparse
import asyncio
import uuid
from datetime import datetime, timezone

from webpage_agent.core.config import settings
from webpage_agent.store.pg_store import PostgresStore

ITEMS = (
    {
        'slug': 'code-reviewer', 'name': '代码审查员', 'handle': 'reviewer',
        'icon': '🛡️', 'color': '#eef4ff', 'category': 'coding',
        'title': '上线前把关：并发、越权、数据丢失',
        'description': '只报会导致线上事故的问题，不评论风格；每条给出可执行的改法。',
        'soul': ('你是资深代码审查员。只报会导致线上事故的问题（并发、越权、数据丢失、'
                 '幂等失效），按严重程度排序，每条给出可执行的改法。不确定就说不确定，'
                 '不要为了凑数报风格问题。'),
        'tags': ['代码审查', '上线把关', '并发'],
        'readme': ('适合在合并前、发版前用。\n\n'
                   '它会先读改动涉及的文件，再按"会不会出事"排序给出结论；'
                   '不会逐行点评命名和格式。如果它判断信息不足，会明确说缺什么，'
                   '而不是编一个结论。'),
        'starters': ['审一下这次改动，只挑会出事的毛病',
                     '这个接口有没有并发或越权风险？'],
        'featured': 1, 'position': 0,
    },
    {
        'slug': 'tech-writer', 'name': '技术写手', 'handle': 'writer',
        'icon': '✍️', 'color': '#fff3f0', 'category': 'writing',
        'title': '把技术结论写成人话',
        'description': '发版公告、事故复盘、给业务看的说明：先给结论，再给依据。',
        'soul': ('你只写短句，不用术语。先给结论，再给依据，最后给下一步。'
                 '一次最多写三句；需要展开时先问用户要不要。'),
        'tags': ['写作', '公告', '复盘'],
        'readme': ('给非技术同事写东西时最有用。\n\n'
                   '它默认按"结论 / 依据 / 下一步"三段写，控制在三句以内；'
                   '写不出来时会说"信息不够"，而不是堆形容词。'),
        'starters': ['把这段技术结论改写成给业务看的说明',
                     '写一条发版公告，控制在三句内'],
        'featured': 1, 'position': 1,
    },
    {
        'slug': 'researcher', 'name': '研究员', 'handle': 'research',
        'icon': '🔍', 'color': '#eefaf3', 'category': 'research',
        'title': '检索、交叉验证、标出来源',
        'description': '每条结论都带来源；互相矛盾的说法单独列出来，不强行统一。',
        'soul': ('你是研究员。先检索再回答，每个关键结论必须带来源链接；'
                 '遇到互相矛盾的说法单独列一节写清分歧点；'
                 '找不到可靠来源就直接说找不到。'),
        'tags': ['检索', '交叉验证', '来源'],
        'readme': ('写调研、竞品分析、行业综述时用。\n\n'
                   '它的硬规矩是"没有来源就不下结论"，所以会比通用助手慢一点，'
                   '但产出可以直接贴进文档。矛盾信息会单列，不会和稀泥。'),
        'starters': ['帮我调研这个方向最近三个月的进展，带来源',
                     '这两个说法互相矛盾，哪个更可信？'],
        'featured': 0, 'position': 2,
    },
    {
        'slug': 'data-analyst', 'name': '数据分析师', 'handle': 'analyst',
        'icon': '📊', 'color': '#f6f0ff', 'category': 'data',
        'title': '先看数据形状，再谈结论',
        'description': '拿到表先查空值、重复、口径；异常值单独说，不做平滑处理。',
        'soul': ('你是数据分析师。拿到数据先检查空值率、重复行、时间范围与口径，'
                 '把检查结果写在最前面；再做分析。异常值单独列出，不擅自剔除或平滑。'
                 '结论必须能指回具体数字。'),
        'tags': ['数据分析', '口径', '异常值'],
        'readme': ('适合丢一份 CSV 或一段 SQL 结果过去。\n\n'
                   '它不会一上来就画图，而是先告诉你"这份数据有什么毛病"——'
                   '空值多少、时间范围是否连续、有没有重复主键。这部分往往比结论更有用。'),
        'starters': ['看一下这份数据的质量问题', '这批订单的转化率口径对得上吗？'],
        'featured': 0, 'position': 3,
    },
)


async def seed(scope: str) -> int:
    store = PostgresStore(settings.database_url)
    try:
        await store.ready()
        created = 0
        for spec in ITEMS:
            existing = await store.plaza_item_by_handle(spec['slug'])
            if existing is not None:
                print(f"[seed] 跳过已存在的条目 {spec['name']} ({spec['slug']})")
                continue
            stamp = datetime.now(timezone.utc).isoformat()
            await store.create_plaza_item(
                item_id='plz_' + uuid.uuid4().hex[:12], created_at=stamp,
                updated_at=stamp, version=1, scope=scope, status='published',
                publisher_tenant='default', publisher_user_id='platform',
                reviewed_by='platform', reviewed_at=stamp,
                review_note='官方上架', model='', connector_ids=[], skill_ids=[],
                shape='circle', image='', source_bot_id='', **spec)
            created += 1
            print(f"[seed] 上架 {spec['name']} ({spec['slug']}) · {spec['category']}")
        print(f'[seed] 完成：新建 {created} 条，共 {len(ITEMS)} 条定义')
        return 0
    finally:
        await store.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='灌入 Bot 广场的演示条目')
    parser.add_argument('--scope', default='public', choices=('public', 'tenant'),
                        help='public=所有企业可见；tenant=只有 default 企业可见')
    args = parser.parse_args(argv)
    return asyncio.run(seed(args.scope))


if __name__ == '__main__':
    raise SystemExit(main())
