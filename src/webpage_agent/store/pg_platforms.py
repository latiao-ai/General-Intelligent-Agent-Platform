"""登录平台登记表：控制台里"能加哪些平台"的数据层。

## 为什么要有这张表

在此之前，中台只有一种账号密码登录：`BEEFTV_API_BASE` 指向 beeftv 的
`/auth/login`。想接第二个平台，得改 `.env` 再重启——这不该是运维要做的事。
这张表把"能拿哪个平台的账号登录中台"变成数据，控制台里增删即时生效。

## 三条不变式

1. **`platform_id` 与 `identity_provider_id` 不是一回事**。前者是入口（写进审计、
   会话），后者决定身份归属（`identities` 的唯一键）。同一 IdP 下开两个入口，
   必须填同一个 `identity_provider_id`，否则同一个人会有两个中台账号；
2. **至少留一个启用的平台**。删/停最后一个，登录入口就没了，而进控制台本身
   就要先登录——这是能把所有人锁在门外的操作，必须在 SQL 前拦住；
3. **默认平台唯一**。`is_default` 为真的行最多一条；设新的会自动清掉旧的，
   在同一个事务里做，避免并发下出现两个默认。

写操作全部落审计（`events` 表 `audit_` 前缀），与 `pg_admin` 一致：
`self._audit()` 来自 `PgAdmin`，所以这个 mixin 必须和它装在同一个 Store 上。
"""
import json
import re
from datetime import datetime, timezone

_PRIMARY_MODE = 'password_http'
# 支持登录方式。目前只有"把账号密码转给平台自己的登录接口校验"这一种；
# 票据/OIDC 走的是另一个模块（auth_connections / oidc），不在这张表里。
LOGIN_MODES = (_PRIMARY_MODE,)

_SLUG = re.compile(r'^[a-z][a-z0-9._-]{1,47}$')
# 取值路径：`data.user.id` 这种点路径。不带斜杠——带斜杠的是 URL 路径。
_PATH = re.compile(r'^[A-Za-z0-9_.\-]{1,64}$')
# 登录路径：必须以 `/` 开头的一段 URL path。不许 `..`，否则能跳出根地址的前缀。
_URL_PATH = re.compile(r'^/[A-Za-z0-9._\-/]{0,63}$')

# `config` 的默认值 = beeftv 的形状。新平台不改这些也能直接跑，
# 形状不同的平台再逐项覆盖——比一上来就要求填七个字段现实。
DEFAULT_CONFIG = {
    'login_path': '/auth/login',
    'username_field': 'username',
    'password_field': 'password',
    'subject_path': 'data.user.id',
    'display_name_path': 'data.user.username',
    'status_path': 'data.user.status',
    'role_path': 'data.user.role',
    'timeout_seconds': 10,
}
_TEXT_KEYS = ('login_path', 'username_field', 'password_field',
              'subject_path', 'display_name_path', 'status_path', 'role_path')


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_config(raw) -> dict:
    """把外部传入的 `config` 收成已知字段 + 默认值。

    不认识的键**丢掉**而不是存下去：这些值最终会被拼进 HTTP 请求，
    留一个没人读的自由字段只会变成下一个人的困惑。空字符串也当没填。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or '{}')
        except ValueError as exc:
            raise ValueError('config 不是合法 JSON') from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError('config 必须是对象')
    out = dict(DEFAULT_CONFIG)
    for key in _TEXT_KEYS:
        value = raw.get(key)
        if value is None or str(value).strip() == '':
            continue
        text = str(value).strip()
        if key == 'login_path':
            if not _URL_PATH.match(text) or '..' in text:
                raise ValueError('login_path 必须是 / 开头的路径，如 /auth/login')
        elif not _PATH.match(text):
            raise ValueError(f'{key} 只能是字母、数字、点、下划线、连字符')
        out[key] = text
    timeout = raw.get('timeout_seconds')
    if timeout is not None and str(timeout).strip() != '':
        try:
            out['timeout_seconds'] = max(1, min(30, int(float(timeout))))
        except (TypeError, ValueError) as exc:
            raise ValueError('timeout_seconds 必须是数字') from exc
    return out


def _decode(row) -> dict:
    """行 → 字典。`config` 坏掉不能让整页 500，退回默认值。"""
    item = dict(row)
    raw = item.get('config') or '{}'
    try:
        item['config'] = normalize_config(raw)
    except ValueError:
        item['config'] = dict(DEFAULT_CONFIG)
    item['enabled'] = bool(item.get('enabled'))
    item['is_default'] = bool(item.get('is_default'))
    item['login_path'] = item['config']['login_path']
    return item


class PgPlatforms:
    """登录平台的增删改查。写操作带审计，读操作只给准入层已放行的调用方用。"""

    # ---------- 读 ----------

    async def platform_list(self) -> dict:
        """全部平台 + 每个平台已经绑了多少账号（按身份源聚合）。

        账号数用 `identity_provider_id` 统计，不是 `platform_id`：同一个 IdP 下的
        两个入口看到的是同一批人，这样才和"这些人能不能登录"对得上。
        """
        async with self._lease() as conn:
            rows = await self._all(
                conn,
                'SELECT platform_id, display_name, login_mode, enabled, is_default, '
                '       tenant_id, identity_provider_id, base_url, config, note, '
                '       created_at, updated_at, created_by, updated_by '
                'FROM auth_platforms ORDER BY is_default DESC, created_at ASC')
            usage = await self._all(
                conn,
                'SELECT identity_provider_id, count(DISTINCT user_id) AS accounts, '
                '       max(last_seen_at) AS last_login_at '
                'FROM identities WHERE identity_provider_id <> \'\' '
                'GROUP BY 1')
        stats = {row['identity_provider_id']: dict(row) for row in usage}
        items = []
        for row in rows:
            item = _decode(row)
            stat = stats.get(item['identity_provider_id'] or '', {})
            item['accounts'] = int(stat.get('accounts') or 0)
            item['last_login_at'] = stat.get('last_login_at') or ''
            items.append(item)
        return {'platforms': items, 'total': len(items)}

    async def platform_get(self, platform_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(
                conn,
                'SELECT platform_id, display_name, login_mode, enabled, is_default, '
                '       tenant_id, identity_provider_id, base_url, config, note, '
                '       created_at, updated_at, created_by, updated_by '
                'FROM auth_platforms WHERE platform_id=%s', (platform_id,))
        return _decode(row) if row else None

    async def platform_login_config(self, platform_id: str = '') -> dict | None:
        """登录热路径要的**最小**配置。只返回启用的平台。

        `platform_id` 为空时：优先默认平台，否则取最早创建的启用平台。
        没有默认也没有平台时返回 None，调用方再决定要不要退回环境变量。
        """
        async with self._lease() as conn:
            if platform_id:
                row = await self._one(
                    conn,
                    'SELECT * FROM auth_platforms WHERE platform_id=%s AND enabled',
                    (platform_id,))
            else:
                row = await self._one(
                    conn,
                    'SELECT * FROM auth_platforms WHERE enabled '
                    'ORDER BY is_default DESC, created_at ASC LIMIT 1')
        return _decode(row) if row else None

    async def platform_public_list(self) -> list[dict]:
        """登录页要的选项：只给启用平台的名字，别把 base_url 之类吐给未登录的人。"""
        async with self._lease() as conn:
            rows = await self._all(
                conn,
                'SELECT platform_id, display_name FROM auth_platforms '
                'WHERE enabled ORDER BY is_default DESC, created_at ASC')
        return [dict(row) for row in rows]

    # ---------- 写 ----------

    async def platform_create(self, data: dict, actor_id: str = '') -> dict:
        """新增平台。第一个平台自动成为默认。"""
        platform_id = str(data.get('platform_id') or '').strip().lower()
        if not _SLUG.match(platform_id):
            raise ValueError('平台标识只能用小写字母开头，含字母/数字/点/下划线/连字符，2-48 位')
        if platform_id in ('', 'default'):
            raise ValueError('平台标识不能是 default')
        mode = str(data.get('login_mode') or _PRIMARY_MODE).strip()
        if mode not in LOGIN_MODES:
            raise ValueError(f'登录方式只支持 {list(LOGIN_MODES)}')
        base_url = str(data.get('base_url') or '').strip().rstrip('/')
        if mode == _PRIMARY_MODE and not base_url:
            raise ValueError('账号密码登录必须填平台接口根地址')
        config = normalize_config(data.get('config'))
        tenant_id = str(data.get('tenant_id') or '').strip() or 'default'
        idp = str(data.get('identity_provider_id') or '').strip() or platform_id
        if len(idp) > 80:
            raise ValueError('身份源标识太长')
        display_name = str(data.get('display_name') or '').strip()[:80] or platform_id
        note = str(data.get('note') or '').strip()[:200]
        stamp = _stamp()
        async with self._lease() as conn:
            exists = await self._one(
                conn, 'SELECT 1 AS ok FROM auth_platforms WHERE platform_id=%s',
                (platform_id,))
            if exists:
                raise FileExistsError('平台标识已存在')
            any_row = await self._one(
                conn, 'SELECT count(*) AS n FROM auth_platforms')
            is_default = bool(data.get('is_default')) or int(
                (any_row or {}).get('n') or 0) == 0
            if is_default:
                await self._run(
                    conn, 'UPDATE auth_platforms SET is_default=false '
                          'WHERE is_default AND platform_id<>%s', (platform_id,))
            await self._run(
                conn,
                'INSERT INTO auth_platforms (platform_id, display_name, login_mode, '
                ' enabled, is_default, tenant_id, identity_provider_id, base_url, '
                ' config, note, created_at, updated_at, created_by, updated_by) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (platform_id, display_name, mode, bool(data.get('enabled', True)),
                 is_default, tenant_id, idp, base_url,
                 json.dumps(config, ensure_ascii=False), note,
                 stamp, stamp, actor_id, actor_id))
            await self._audit(conn, {
                'type': 'console_platform_create', 'operator_id': actor_id,
                'platform_id': platform_id, 'display_name': display_name,
                'login_mode': mode, 'tenant_id': tenant_id,
                'identity_provider_id': idp, 'base_url': base_url,
                'is_default': is_default}, stamp)
            row = await self._one(
                conn, 'SELECT * FROM auth_platforms WHERE platform_id=%s',
                (platform_id,))
        return _decode(row)

    async def platform_update(self, platform_id: str, patch: dict,
                              actor_id: str = '') -> dict:
        """改平台。`platform_id` 不可改——它是审计与会话里的历史值。"""
        fields, params, changed = [], [], []
        async with self._lease() as conn:
            current = await self._one(
                conn, 'SELECT * FROM auth_platforms WHERE platform_id=%s',
                (platform_id,))
            if current is None:
                raise LookupError('平台不存在')
            before = _decode(current)

            if 'display_name' in patch:
                value = str(patch.get('display_name') or '').strip()[:80]
                fields.append('display_name=%s')
                params.append(value or platform_id)
                changed.append('display_name')
            if 'base_url' in patch:
                value = str(patch.get('base_url') or '').strip().rstrip('/')
                if before['login_mode'] == _PRIMARY_MODE and not value:
                    raise ValueError('账号密码登录必须填平台接口根地址')
                fields.append('base_url=%s')
                params.append(value)
                changed.append('base_url')
            if 'tenant_id' in patch:
                value = str(patch.get('tenant_id') or '').strip() or 'default'
                fields.append('tenant_id=%s')
                params.append(value)
                changed.append('tenant_id')
            if 'identity_provider_id' in patch:
                # 改身份源等于"让之后登录的人落到另一批账号上"，存量绑定不会跟着走，
                # 所以只允许改，但在审计里记下改前改后。
                value = str(patch.get('identity_provider_id') or '').strip() or platform_id
                if len(value) > 80:
                    raise ValueError('身份源标识太长')
                fields.append('identity_provider_id=%s')
                params.append(value)
                changed.append('identity_provider_id')
            if 'config' in patch:
                config = normalize_config(patch.get('config'))
                fields.append('config=%s')
                params.append(json.dumps(config, ensure_ascii=False))
                changed.append('config')
            if 'note' in patch:
                fields.append('note=%s')
                params.append(str(patch.get('note') or '').strip()[:200])
                changed.append('note')
            if 'enabled' in patch:
                enabled = bool(patch.get('enabled'))
                if not enabled:
                    await self._assert_not_last_enabled(
                        conn, platform_id, currently_enabled=before['enabled'])
                    # 停用默认平台时把"默认"交给下一个启用的平台，否则登录页会
                    # 默认选中一个不能用的入口（`is_default` 只看标志不看 enabled）。
                    if before['is_default']:
                        fields.append('is_default=false')
                        await self._promote_default(conn, exclude=platform_id)
                fields.append('enabled=%s')
                params.append(enabled)
                changed.append('enabled')
            if 'is_default' in patch:
                if patch.get('is_default'):
                    if patch.get('enabled') is False:
                        raise ValueError('默认平台必须处于启用状态')
                    fields.append('is_default=true')
                    changed.append('is_default')
                    await self._run(
                        conn, 'UPDATE auth_platforms SET is_default=false '
                              'WHERE is_default AND platform_id<>%s', (platform_id,))
                    # 成为默认必须同时启用，否则登录页默认选了个不能用的入口
                    if 'enabled' not in patch:
                        fields.append('enabled=true')
                        params.append(True)
                elif before['is_default']:
                    # 主动取消默认：交给下一个启用的平台，不留空
                    fields.append('is_default=false')
                    changed.append('is_default')
                    await self._promote_default(conn, exclude=platform_id)
            if not fields:
                return before

            params.extend([_stamp(), actor_id, platform_id])
            await self._run(
                conn,
                f'UPDATE auth_platforms SET {", ".join(fields)}, '
                'updated_at=%s, updated_by=%s WHERE platform_id=%s', tuple(params))
            row = await self._one(
                conn, 'SELECT * FROM auth_platforms WHERE platform_id=%s',
                (platform_id,))
            after = _decode(row)
            await self._audit(conn, {
                'type': 'console_platform_update', 'operator_id': actor_id,
                'platform_id': platform_id, 'fields': changed,
                'before': {key: before.get(key) for key in changed},
                'after': {key: after.get(key) for key in changed}}, _stamp())
        return after

    async def platform_delete(self, platform_id: str, actor_id: str = '') -> dict:
        """删平台。**不删**已经建立的账号与身份绑定——那些人的历史会话还在。

        代价说清楚：删完之后这个入口登不进来了，但账号本身还在，用户列表里
        照样能看到他们（`identities` 里仍留着身份源）。
        """
        async with self._lease() as conn:
            row = await self._one(
                conn, 'SELECT * FROM auth_platforms WHERE platform_id=%s',
                (platform_id,))
            if row is None:
                raise LookupError('平台不存在')
            target = _decode(row)
            await self._assert_not_last_enabled(
                conn, platform_id, currently_enabled=target['enabled'])
            accounts = await self._one(
                conn,
                'SELECT count(DISTINCT user_id) AS n FROM identities '
                'WHERE identity_provider_id=%s', (target['identity_provider_id'],))
            await self._run(
                conn, 'DELETE FROM auth_platforms WHERE platform_id=%s', (platform_id,))
            if target['is_default']:
                # 默认平台被删掉后不能留空：登录页总要有个默认选项
                await self._promote_default(conn, exclude=platform_id)
            await self._audit(conn, {
                'type': 'console_platform_delete', 'operator_id': actor_id,
                'platform_id': platform_id,
                'display_name': target['display_name'],
                'identity_provider_id': target['identity_provider_id'],
                'retained_accounts': int((accounts or {}).get('n') or 0)}, _stamp())
        return {'platform_id': platform_id, 'deleted': True,
                'retained_accounts': int((accounts or {}).get('n') or 0)}

    async def _promote_default(self, conn, *, exclude: str = '') -> None:
        """把"默认"交给最早创建的那个启用平台。没有候选就保持原样。"""
        await self._run(
            conn,
            'UPDATE auth_platforms SET is_default=true WHERE platform_id=('
            '  SELECT platform_id FROM auth_platforms '
            '  WHERE enabled AND platform_id<>%s '
            '  ORDER BY created_at ASC LIMIT 1) AND NOT is_default', (exclude,))

    async def _assert_not_last_enabled(self, conn, platform_id: str, *,
                                       currently_enabled: bool = True) -> None:
        """删除/停用前的守门：别把最后一个启用的入口关掉。

        已经在停用状态的平台不占名额——删它不会减少可用入口，拦住只是让人
        清理不掉脏数据。
        """
        if not currently_enabled:
            return
        row = await self._one(
            conn,
            'SELECT count(*) AS n FROM auth_platforms '
            'WHERE enabled AND platform_id<>%s', (platform_id,))
        if int((row or {}).get('n') or 0) == 0:
            raise ValueError('这是最后一个启用的登录平台，停掉就没人能登录了')
