"""平台级技能与连接器目录：管理员发布一次，所有用户默认可见。

## 它解决什么

在此之前，"有哪些连接器"只由 `MCP_SERVERS`（运维配置）和 `profile/mcp.json`
（用户自建）决定，"有哪些技能"只由静态 `catalog.py` 和磁盘技能决定。管理员
想给所有人加一个连接器，只能改 `.env` 重启；想加一个技能，只能往
`SKILLS_DIR` 里丢目录——两条路都不在控制台里，也没法停用。

这个模块补上中间那层：

- **连接器**：`mcp_servers.servers()` 会把这些条目当作真实服务端合并进去，
  于是工具发现、连接、OAuth 授权全链路自动生效，不需要各自打补丁；
- **技能**：`skillhub_api` 把它们并进目录，用户技能页直接多一张卡片。

## 为什么是文件而不是库表

这两类东西的事实来源本来就在文件里（`MCP_SERVERS` / `SKILLS_DIR`）。再开一张
表就得处理"表里有、文件里没有"的双写不一致，而这里唯一的写者是控制台。
一把 `flock` + `os.replace` 就够，且改完下一轮请求立刻生效，不用迁移。

## 与系统配置的优先级

`MCP_SERVERS` 里的同名服务端**优先**：运维在环境变量里写死的东西不能被控制台
悄悄覆盖掉鉴权头。冲突时控制台条目会被跳过，并在列表接口里标出来。
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from webpage_agent.core.config import settings

_ROOT = Path(__file__).resolve().parents[3]
# 与 `MCP_USER_CONFIG_FILE` 同样的模式：允许指向别处。
# 这不只是给测试用的——部署时把可变数据放到挂载卷上，代码目录可以只读。
_CONFIG_ENV = 'PLATFORM_CATALOG_FILE'


def _default_path() -> Path:
    raw = os.getenv(_CONFIG_ENV, '').strip()
    if raw:
        return Path(raw).expanduser()
    return _ROOT / 'profile' / 'skillhub' / 'platform_catalog.json'


_PATH = _default_path()

# 连接器 id 同时会进工具名前缀（`mcp__<id>__<tool>`），所以不许出现 `__`。
_ID_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')
# 控制台只收 HTTP/SSE：stdio 要在中台机器上起进程，属于运维配置而不是
# "管理员在页面上填一个 URL"。放开它等于把网页表单变成任意命令执行。
_TRANSPORTS = ('streamable_http', 'sse')
_AUTH_MODES = ('', 'oauth')


class CatalogError(ValueError):
    """条目字段不合法。`status` 供 API 层直接映射成 HTTP 状态码。"""

    def __init__(self, detail: str, *, status: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


# ---------------------------------------------------------------- 文件读写


@contextmanager
def _locked():
    """跨进程串行写。读也加共享锁，避免读到一半被替换。

    `os.replace` 已经保证"不会读到半份 JSON"，这里的锁是为了防止两个管理员
    同时点保存时互相覆盖（读-改-写的经典丢更新）。
    """
    # 每次现读环境变量：测试与「改配置立刻生效」都依赖这一点，等价于
    # mcp_servers.user_config_path() 的行为。
    path = _default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix('.lock')
    handle = open(lock_path, 'a+')
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield path
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _empty() -> dict:
    """每次都给**新的**列表。

    `dict(_EMPTY)` 只复制外层：两个列表仍是同一个对象，`upsert` 里的
    `rows.append()` 会把条目追加进这个"空"模板。之后任何一次读到缺失文件，
    都会带着上一次的数据——读空文件却拿到别人的行，排查起来极难。
    """
    return {'connectors': [], 'skills': []}


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    return {
        'connectors': [x for x in (data.get('connectors') or [])
                       if isinstance(x, dict) and x.get('id')],
        'skills': [x for x in (data.get('skills') or [])
                   if isinstance(x, dict) and x.get('id')],
    }


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n',
                   encoding='utf-8')
    os.replace(tmp, path)


def load() -> dict:
    """全量读取（含停用条目），控制台列表用。"""
    with _locked() as path:
        return _read(path)


def save(data: dict) -> None:
    with _locked() as path:
        _write(path, data)


# ---------------------------------------------------------------- 校验


def _clean_id(raw: str, *, what: str) -> str:
    value = str(raw or '').strip().lower()
    if not _ID_RE.match(value):
        raise CatalogError(f'{what} ID 只能是小写字母/数字/._-，且不超过 64 位'
                           f'（收到 {raw!r}）')
    if '__' in value:
        raise CatalogError(f'{what} ID 不能包含 "__"：工具名用它分隔服务端与工具')
    return value


def _clean_text(raw, limit: int) -> str:
    return str(raw or '').strip()[:limit]


def _clean_examples(raw) -> list[str]:
    """「试试这样用」：去空、去重、限长限量。

    这些字符串会原样显示成用户点的按钮，也会写进 SKILL.md——空串或重复项在
    界面上就是一排空白按钮，所以在落盘前就收敛掉。
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        text = _clean_text(item, 120)
        if text and text not in out:
            out.append(text)
        if len(out) >= 8:
            break
    return out


def validate_connector(payload: dict) -> dict:
    """把一条连接器表单收敛成可落盘的形态。缺什么当场报，不等到用户点连接。"""
    item_id = _clean_id(payload.get('id'), what='连接器')
    name = _clean_text(payload.get('name'), 80) or item_id
    transport = _clean_text(payload.get('transport'), 40) or 'streamable_http'
    if transport not in _TRANSPORTS:
        raise CatalogError('传输方式只支持 streamable_http / sse；'
                           '本地进程类 stdio 请配置 MCP_SERVERS')
    url = _clean_text(payload.get('url'), 500)
    if not url.startswith(('http://', 'https://')):
        raise CatalogError('连接器地址必须以 http:// 或 https:// 开头')

    headers_raw = payload.get('headers') or {}
    if not isinstance(headers_raw, dict):
        raise CatalogError('请求头必须是键值对象')
    headers: dict[str, str] = {}
    for key, value in headers_raw.items():
        key = str(key).strip()
        if not key:
            continue
        if not isinstance(value, str):
            raise CatalogError(f'请求头 {key} 的值必须是字符串')
        # 换行会变成 header 注入，直接拒掉。
        if '\n' in key or '\r' in key or '\n' in value or '\r' in value:
            raise CatalogError(f'请求头 {key} 含有换行符')
        headers[key] = value

    auth = _clean_text(payload.get('auth'), 20).lower()
    if auth not in _AUTH_MODES:
        raise CatalogError('授权方式只能是「静态凭据」或「OAuth」')
    issuer = _clean_text(payload.get('oauth_issuer'), 300)
    if auth == 'oauth' and not issuer.startswith(('http://', 'https://')):
        raise CatalogError('OAuth 授权需要填 issuer 地址（https://…）')

    return {
        'id': item_id,
        'name': name,
        'description': _clean_text(payload.get('description'), 240),
        'icon': _clean_text(payload.get('icon'), 8) or '🔗',
        'transport': transport,
        'url': url,
        'headers': headers,
        'auth': auth,
        'oauth_issuer': issuer,
        'oauth_scopes': [_clean_text(x, 60) for x in (payload.get('oauth_scopes') or [])
                         if _clean_text(x, 60)],
        'enabled': bool(payload.get('enabled', True)),
    }


def validate_skill(payload: dict, categories: tuple[str, ...]) -> dict:
    item_id = _clean_id(payload.get('id'), what='技能')
    name = _clean_text(payload.get('name'), 40)
    if not name:
        raise CatalogError('技能名称不能为空')
    category = _clean_text(payload.get('category'), 40) or '效率工具'
    if categories and category not in categories:
        raise CatalogError(f'未知分类：{category}')
    return {
        'id': item_id,
        'name': name,
        'description': _clean_text(payload.get('description'), 240),
        'category': category,
        # 图标可以是 emoji，也可以是控制台上传的图片（data URL）。上限跟
        # console_api._SKILL_ICON_MAX 对齐。
        'icon': _clean_text(payload.get('icon'), 300_000) or '✨',
        'version': _clean_text(payload.get('version'), 20) or '1.0.0',
        # 概述 = 技能详情页展开的那段介绍，比「一句话简介」长，比 prompt 短。
        'overview': _clean_text(payload.get('overview'), 600),
        'examples': _clean_examples(payload.get('examples')),
        'prompt': _clean_text(payload.get('prompt'), 20_000),
        'enabled': bool(payload.get('enabled', True)),
        # 默认安装 = 用户第一次打开技能页时就已经可用，不必再点「+」。
        'installed_by_default': bool(payload.get('installed_by_default', False)),
    }


# ---------------------------------------------------------------- 增删改


def _stamp() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _mutate(kind: str, change):
    """在同一把文件锁里完成「读 → 改 → 写」。

    写操作必须整段持锁：`load()` 和 `save()` 各自加锁看似安全，实际是两个
    临界区，中间那段窗口足够让另一个管理员的保存被覆盖掉。
    """
    if kind not in ('connectors', 'skills'):
        raise CatalogError('未知目录类型')
    with _locked() as path:
        data = _read(path)
        result = change(data[kind])
        _write(path, data)
        return result


def upsert(kind: str, payload: dict, *, actor: str = '') -> dict:
    """新增或整体替换一条。存在则保留 created_at / created_by。"""
    item_id = payload['id']
    now = _stamp()

    def change(rows: list) -> dict:
        for index, row in enumerate(rows):
            if row.get('id') != item_id:
                continue
            merged = {**row, **payload,
                      'created_at': row.get('created_at') or now,
                      'created_by': row.get('created_by') or actor,
                      'updated_at': now, 'updated_by': actor}
            rows[index] = merged
            return merged
        created = {**payload, 'created_at': now, 'created_by': actor,
                   'updated_at': now, 'updated_by': actor}
        rows.append(created)
        return created

    return _mutate(kind, change)


def set_skill_package(item_id: str, *, package: str, version: str = '',
                      files: int = 0, size: int = 0, filename: str = '',
                      digest: str = '', actor: str = '') -> dict:
    """记下技能包原件的位置（**只记路径，不存字节**）。

    包可能有几 MB，塞进这份 JSON 会让每次列技能都读一遍二进制。这里只留
    "在哪、多大、包里几个文件"，安装时再解。

    `package` 存**相对包仓库**（`skillhub_packages_dir`）的路径。存绝对路径
    在本地跑得好好的，一旦搬到服务器——换了个目录、换了台机器——整份 JSON 里
    每个包都指向不存在的文件，而安装又会"找不到包就按正文生成"，于是表现为
    技能包静默失效、还没人报错。相对路径跟着包仓库走，拷过去就能用。

    `filename` 是管理员上传时的原始文件名：磁盘上按 `<版本><扩展名>` 存，
    回显时用原名，人才认得出"这是我上次传的那个包"。
    """
    def change(rows: list) -> dict:
        for row in rows:
            if row.get('id') != item_id:
                continue
            row.update({
                'package': package,
                'package_version': version,
                'package_files': int(files),
                'package_bytes': int(size),
                'package_name': str(filename or ''),
                'package_digest': str(digest or ''),
                'updated_at': _stamp(),
                'updated_by': actor,
            })
            return row
        raise CatalogError('条目不存在', status=404)

    return _mutate('skills', change)


def set_package_digest(item_id: str, digest: str) -> None:
    """只补指纹。存量条目是上线前传的、没有摘要，第一次用到时按文件算一次补上。

    单独一个函数是为了不连带改 `updated_by`：补摘要不是人的动作。
    """
    def change(rows: list) -> dict:
        for row in rows:
            if row.get('id') != item_id:
                continue
            row['package_digest'] = str(digest or '')
            return row
        raise CatalogError('条目不存在', status=404)

    _mutate('skills', change)


def package_path(raw: str) -> Path | None:
    """目录里记的包位置 → 磁盘上的实际文件；找不到返回 `None`。

    新数据是相对路径（相对 `skillhub_packages_dir`），老数据是绝对路径——
    两种都要认：存量条目不能因为改了存储格式就集体失效。
    """
    text = str(raw or '').strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path(settings.skillhub_packages_dir) / path
    return path if path.is_file() else None


def clear_skill_package(item_id: str, *, actor: str = '') -> dict:
    """删掉包记录，技能退回「只有正文」的形态。"""
    def change(rows: list) -> dict:
        for row in rows:
            if row.get('id') != item_id:
                continue
            for key in ('package', 'package_version', 'package_files',
                        'package_bytes', 'package_name', 'package_digest'):
                row.pop(key, None)
            row['updated_at'] = _stamp()
            row['updated_by'] = actor
            return row
        raise CatalogError('条目不存在', status=404)

    return _mutate('skills', change)


def set_enabled(kind: str, item_id: str, enabled: bool, *, actor: str = '') -> dict:
    def change(rows: list) -> dict:
        for row in rows:
            if row.get('id') != item_id:
                continue
            row['enabled'] = bool(enabled)
            row['updated_at'] = _stamp()
            row['updated_by'] = actor
            return row
        raise CatalogError('条目不存在', status=404)

    return _mutate(kind, change)


def remove(kind: str, item_id: str) -> dict:
    def change(rows: list) -> dict:
        kept = [row for row in rows if row.get('id') != item_id]
        if len(kept) == len(rows):
            raise CatalogError('条目不存在', status=404)
        rows[:] = kept
        return {'id': item_id, 'deleted': True}

    return _mutate(kind, change)


# ---------------------------------------------------------------- 消费侧


def enabled_connectors() -> list[dict]:
    """给 `mcp_servers` 用的连接器条目（只要启用的）。"""
    return [row for row in load()['connectors'] if row.get('enabled', True)]


def enabled_skills() -> list[dict]:
    """给技能中心目录用的技能条目（只要启用的）。"""
    return [row for row in load()['skills'] if row.get('enabled', True)]
