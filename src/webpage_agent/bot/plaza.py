"""Bot 广场的纯函数与产品口径：分类、状态、可见性、两份视图。

**为什么单独一个模块**：这三样东西被三个地方同时需要——用户接口
（`api/plaza_api.py`）、管理控制台（`api/console_api.py`）、仓储白名单
（`store/pg_plaza.py`）。任何一个地方自己抄一份，第一次改动就会让"广场上看得见
但控制台说下架了"这种矛盾出现。这里不碰数据库、不碰请求，全部可直接单测。

状态机（`status`）：

```
draft ──提交──▶ pending ──通过──▶ published ──下架──▶ offline
                   │                   ▲                 │
                   └──驳回──▶ rejected ─┴──重新提交───────┘
```

只有 `published` 会被用户看到。`rejected` 必须带理由（`review_note`），
否则投稿人只知道"没过"，不知道该改哪。
"""
from __future__ import annotations

# 分类是产品口径，写死在这里而不是从数据里 distinct 出来：分类列表频繁增删会让
# 广场的筛选条一会儿长一会儿短，用户找不到刚才那个入口。新分类走一次代码改动。
CATEGORIES = (
    ('writing', '写作'),
    ('coding', '研发'),
    ('research', '研究'),
    ('data', '数据'),
    ('ops', '运维'),
    ('marketing', '市场'),
    ('product', '产品'),
    ('general', '通用'),
)
CATEGORY_IDS = tuple(key for key, _ in CATEGORIES)
CATEGORY_NAMES = dict(CATEGORIES)

STATUSES = ('draft', 'pending', 'published', 'rejected', 'offline')
VISIBLE_STATUS = 'published'
SCOPES = ('tenant', 'public')

MAX_TAGS = 8
MAX_STARTERS = 6
MAX_README = 20_000


def normalize_category(value: str) -> str:
    text = str(value or '').strip()
    return text if text in CATEGORY_IDS else 'general'


def normalize_status(value: str, default: str = 'published') -> str:
    text = str(value or '').strip()
    return text if text in STATUSES else default


def clean_tags(tags) -> list[str]:
    out: list[str] = []
    for tag in tags or ():
        text = str(tag or '').strip()[:20]
        if text and text not in out:
            out.append(text)
    return out[:MAX_TAGS]


def clean_starters(starters) -> list[str]:
    out: list[str] = []
    for line in starters or ():
        text = str(line or '').strip()[:200]
        if text and text not in out:
            out.append(text)
    return out[:MAX_STARTERS]


def visible_to(item: dict, tenant: str) -> bool:
    """一条目对某个租户是否可见：必须已上架，并且是公开的或同企业投稿的。"""
    if str((item or {}).get('status') or '') != VISIBLE_STATUS:
        return False
    if str(item.get('scope') or 'tenant') == 'public':
        return True
    return str(item.get('publisher_tenant') or '') == str(tenant or '')


def list_view(item: dict, *, installed_bot_id: str = '') -> dict:
    """列表卡片：**不带** soul / readme / starters。

    一页 60 条，这些是详情才要的字段；全塞进列表会让首屏多几百 KB，
    而用户点开之前根本用不到它们。
    """
    item = item or {}
    return {
        'item_id': item.get('item_id'),
        'slug': item.get('slug') or '',
        'name': item.get('name') or '',
        'handle': item.get('handle') or '',
        'title': item.get('title') or '',
        'description': item.get('description') or '',
        'icon': item.get('icon') or '🤖',
        'color': item.get('color') or '#eef4ff',
        'shape': item.get('shape') or 'circle',
        'image': item.get('image') or '',
        'category': normalize_category(item.get('category')),
        'tags': list(item.get('tags') or []),
        'model': item.get('model') or '',
        'skill_ids': list(item.get('skill_ids') or []),
        'connector_ids': list(item.get('connector_ids') or []),
        'install_count': int(item.get('install_count') or 0),
        'featured': bool(item.get('featured')),
        'version': int(item.get('version') or 1),
        'installed': bool(installed_bot_id),
        'installed_bot_id': installed_bot_id or '',
        'published_at': item.get('reviewed_at') or item.get('created_at') or '',
    }


def detail_view(item: dict, *, installed_bot_id: str = '') -> dict:
    view = list_view(item, installed_bot_id=installed_bot_id)
    item = item or {}
    view.update({
        'soul': item.get('soul') or '',
        'readme': item.get('readme') or '',
        'starters': list(item.get('starters') or []),
        'scope': item.get('scope') or 'tenant',
        'status': item.get('status') or '',
        'publisher_user_id': item.get('publisher_user_id') or '',
        'source_bot_id': item.get('source_bot_id') or '',
        'view_count': int(item.get('view_count') or 0),
        'updated_at': item.get('updated_at') or '',
    })
    return view
