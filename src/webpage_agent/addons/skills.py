"""技能库：磁盘上的 `SKILL.md` 包。

## 为什么技能是文件而不是数据库行

WorkBuddy 的技能就是目录（`~/.workbuddy/skills/<name>/SKILL.md` + `references/` +
`scripts/`）。照抄这一点有两个直接好处：

1. **新增技能不需要发版**：放一个目录进去就出现；用户上传也只是一次解包；
2. **不预加载正文**：`SKILL.md` 的 frontmatter（名字 + 描述）进列表与提示，正文由
   模型在需要时自己 `read_file`——这是"少烧 token"的关键，也是本项目的迁移重点。
   把 30 个技能全文塞进系统提示，上下文当天就爆了。

库里不建技能索引表：磁盘是唯一事实来源，建索引就要处理"目录在、表里没有"的
双写不一致。列表每次直接扫目录（几十个技能的 `stat` 代价可以忽略）。

## 目录约定

```
<skills_dir>/
├── .state.json            # {"<id>": {"enabled": false}}，启停软状态
├── deep-research/
│   ├── SKILL.md           # 必需；frontmatter 至少要有 description
│   ├── references/*.md    # 可选，按需读取
│   └── scripts/*.py       # 可选
└── my-upload/
```

启停**不删目录**：软开关只写 `.state.json`，因为"禁用"往往是临时的，删了就丢。

## 会话物化

每一轮只把**当前会话选中的技能**拷进
`<workspace>/.agent-skills/<session>/<id>/`，模型用相对路径 `read_file` 读取。
不物化到全局、也不把技能目录直接暴露给模型：会话结束或下一轮重建时整体替换，
避免上一轮删掉的技能还躺在目录里被读到。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tarfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from webpage_agent.core.config import settings

# 体积上限全部来自配置（`SKILL_MAX_ARCHIVE_MB` / `SKILL_MAX_MEMBER_MB`），
# **默认不限（0）**。以前这里写死 20 MB / 5 MB，理由是"技能是 prompt 包不是数据集"；
# 但真实技能包常带前端依赖、示例数据和图片，写死的数字只会把正常包挡在门外，
# 而护栏该由部署方按自己的内存和带宽定。
#
# 两个上限必须成对出现：只放开整包、单文件还卡着，用户看到的是"包能传、装不上"，
# 比直接拒绝更难解释。所以这里给的是同一套取值函数，调用方不再各自写数字。
def max_archive_bytes() -> int:
    """整包上限（字节）。0 = 不限。"""
    return max(0, int(settings.skill_max_archive_mb)) * 1024 * 1024


def max_member_bytes() -> int:
    """包内单文件上限（字节）。0 = 不限。"""
    return max(0, int(settings.skill_max_member_mb)) * 1024 * 1024


def max_members() -> int:
    """包内文件条数上限。条数是 zip 炸弹的硬指标，与体积无关，始终有值。"""
    return max(1, int(settings.skill_max_members))

_STATE_FILE = '.state.json'
# 每个"由包落地"的技能目录里记一份包指纹：管理端换包之后，靠它判断磁盘上
# 这份是不是当前那个包。隐藏文件，`_file_tree` 不列、模型也读不到。
_PACKAGE_MARK = '.package.json'
_ID_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')
_SKILL_FILE = 'SKILL.md'
# 物化时忽略的东西：缓存、版本库、模型不可能用到的临时文件。
_IGNORE = {'__pycache__', '.git', '.DS_Store', '.pytest_cache', '.ruff_cache'}


class SkillError(ValueError):
    """技能包不合法（缺 SKILL.md、frontmatter 无效、包结构有歧义等）。"""


# ---------------------------------------------------------------- 基础路径


def root() -> Path:
    path = Path(settings.skills_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state() -> dict:
    try:
        data = json.loads((root() / _STATE_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(data: dict) -> None:
    path = root() / _STATE_FILE
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def valid_id(skill_id: str) -> bool:
    return bool(_ID_RE.match(str(skill_id or '')))


def package_digest(payload: bytes) -> str:
    """包的指纹（前 16 位 sha256）。

    为什么不用版本号判断"包换没换"：控制台不让人填版本，换包就是直接覆盖原件，
    版本字段常年停在 `1.0.0`。拿版本比等于永远认为"没变"，用户那份就永远不更新。
    """
    return hashlib.sha256(payload).hexdigest()[:16]


def _read_mark(directory: Path) -> dict:
    try:
        data = json.loads((directory / _PACKAGE_MARK).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_mark(directory: Path, *, digest: str, version: str = '',
                filename: str = '', files: list[str] | None = None) -> None:
    payload = {
        'digest': str(digest or ''),
        'version': str(version or ''),
        'filename': str(filename or ''),
        'files': sorted(str(x) for x in (files or [])),
        'synced_at': time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime()),
    }
    path = directory / _PACKAGE_MARK
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def installed_package(skill_id: str) -> dict:
    """已装目录里记的包指纹；不是由包落地（生成/手写/导入目录）就返回空。"""
    if not valid_id(skill_id):
        return {}
    return _read_mark(root() / skill_id)


def slug(text: str, fallback: str = 'skill') -> str:
    """把任意名字收敛成合法目录名；中文名收敛为空时回退。"""
    raw = str(text or '').strip().lower()
    raw = re.sub(r'[^a-z0-9._-]+', '-', raw).strip('.-_')
    return raw[:64] if valid_id(raw) else fallback


# ---------------------------------------------------------------- SKILL.md


def _plain(value: str) -> str:
    text = str(value or '').strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        text = text[1:-1]
    return text.strip()


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 YAML frontmatter 的**简单子集**（key: value）。

    技能清单里只用到 `name / description / version / license` 这类标量，为了几个
    键引入 yaml 依赖不值得；而 `references/*.md` 里的正文才是需要完整 Markdown
    的部分，那部分原样返回。遇到复杂 YAML（列表、嵌套）按"没有这个键"处理，
    不让解析失败把整个技能变成不可用。
    """
    raw = str(text or '')
    lines = raw.splitlines()
    if not lines or lines[0].strip() != '---':
        return {}, raw
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() in ('---', '...'):
            end = index
            break
    if end is None:
        return {}, raw
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition(':')
        if not sep:
            continue
        name = key.strip()
        if not name:
            continue
        meta[name] = _plain(value)
    return meta, '\n'.join(lines[end + 1:]).lstrip('\n')


def read_skill_md(path: Path) -> tuple[dict, str]:
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as exc:
        raise SkillError(f'读不到 {_SKILL_FILE}：{exc}') from exc
    return parse_frontmatter(text)


def _section_text(body: str, heading: str) -> str:
    """取 `## 标题` 到下一个二级标题之间的正文（多行拼成一段）。"""
    out: list[str] = []
    inside = False
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith('## '):
            if inside:
                break
            inside = line[3:].strip().startswith(heading)
            continue
        if inside and line:
            out.append(line)
    return '\n'.join(out).strip()


def _section_bullets(body: str, heading: str, limit: int = 8) -> list[str]:
    """取 `## 标题` 段里的列表项，用作「试试这样用」。"""
    out: list[str] = []
    inside = False
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith('## '):
            if inside:
                break
            inside = line[3:].strip().startswith(heading)
            continue
        if not inside:
            continue
        hit = re.match(r'^[-*+]\s+(.*)$', line) or re.match(r'^\d+[.)]\s+(.*)$', line)
        if hit and hit.group(1).strip():
            out.append(hit.group(1).strip()[:120])
        if len(out) >= limit:
            break
    return out


def _file_tree(directory: Path, limit: int = 200) -> list[str]:
    """技能目录下的相对文件清单（含子目录，`references/a.md` 这种形态）。

    用显式递归而不是 `rglob('*')`：后者会跟进符号链接，包里放一个指向上级的
    链接就能让扫描原地打转。这里不跟链接，隐藏项与缓存整段跳过；超过 limit
    就截断——详情页只需要知道"装了什么"，不需要一份完整备份清单。
    """
    out: list[str] = []

    def walk(current: Path, prefix: str) -> None:
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        for entry in entries:
            if len(out) >= limit:
                return
            if entry.name.startswith('.') or entry.name in _IGNORE:
                continue
            if entry.is_symlink():
                continue
            if entry.is_dir():
                walk(entry, prefix + entry.name + '/')
            elif entry.is_file():
                out.append(prefix + entry.name)

    walk(directory, '')
    return out


def _meta_of(directory: Path, skill_id: str, state: dict) -> dict | None:
    skill_md = directory / _SKILL_FILE
    if not skill_md.is_file():
        return None
    try:
        meta, body = read_skill_md(skill_md)
    except SkillError:
        return None
    name = str(meta.get('name') or skill_id).strip() or skill_id
    description = str(meta.get('description') or '').strip()
    if not description:
        # 正文第一段当描述：手写技能常常忘了写 frontmatter 的 description。
        first = next((line.strip('# ').strip() for line in body.splitlines()
                      if line.strip() and not line.strip().startswith('#')), '')
        description = first[:160]
    files = sorted(p.name for p in directory.iterdir()
                   if p.name not in _IGNORE and not p.name.startswith('.'))
    # `files` 只有一层，展开后看不出包里有没有 references/scripts。详情页要
    # 显示"这个技能装了什么"，所以再给一份递归清单。
    file_tree = _file_tree(directory)
    mark = _read_mark(directory)
    row = {
        'id': skill_id,
        'skill_id': skill_id,
        'name': name,
        'description': description,
        'version': str(meta.get('version') or '1.0.0'),
        'license': str(meta.get('license') or ''),
        'source': str(meta.get('source') or 'user'),
        'category': str(meta.get('category') or ''),
        'author': str(meta.get('author') or ''),
        # 概述和「试试这样用」写在正文里，但用户端详情页要直接显示，
        # 所以在扫描时顺手取出来（只读一次文件，成本可忽略）。
        'overview': str(meta.get('overview') or '').strip() or _section_text(body, '概述')[:600],
        'examples': _section_bullets(body, '试试这样用'),
        'root_path': str(directory),
        'files': files,
        'file_tree': file_tree,
        # 磁盘上这份来自哪个包：`package_digest` 为空 = 生成的/手写的/导入的，
        # 与管理端的包无关，同步时不该拿它去比。
        'package_digest': str(mark.get('digest') or ''),
        'package_version': str(mark.get('version') or ''),
        'package_synced_at': str(mark.get('synced_at') or ''),
        'package_managed': bool(mark),
        'has_body': bool(body.strip()),
        'enabled': bool((state.get(skill_id) or {}).get('enabled', True)),
        'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S',
                                    time.localtime(skill_md.stat().st_mtime)),
    }
    return row


# ---------------------------------------------------------------- 读取 / 列表


def list_skills() -> list[dict]:
    """扫磁盘列技能。没有 SKILL.md 的目录不算技能（`.state.json` 之类也跳过）。"""
    state = _state()
    items = []
    for entry in sorted(root().iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or entry.is_symlink() or entry.name.startswith('.'):
            continue
        row = _meta_of(entry, entry.name, state)
        if row is not None:
            items.append(row)
    return items


def get_skill(skill_id: str) -> dict | None:
    if not valid_id(skill_id):
        return None
    directory = root() / skill_id
    if not directory.is_dir() or directory.is_symlink():
        return None
    return _meta_of(directory, skill_id, _state())


def _write_skill(skill_id: str, text: str, *, source: str, files: dict[str, bytes] | None = None,
                 name: str = '', description: str = '',
                 package: dict | None = None) -> dict:
    """把一份技能落到 `<skills_dir>/<id>/`。`files` 的键是相对技能根的路径。

    `package` 非空表示"这份是从某个包解出来的"：顺手在目录里记下包指纹，
    之后管理端换包时才能判断要不要覆盖（见 `sync_installed`）。
    """
    if not valid_id(skill_id):
        raise SkillError(f'技能 id 不合法：{skill_id!r}（小写字母数字 _ . -，1~64 位）')
    meta, _ = parse_frontmatter(text)
    if not str(meta.get('description') or description).strip():
        raise SkillError('SKILL.md 的 frontmatter 缺少 description；'
                         '它是技能列表里唯一会进模型上下文的信息')
    target = root() / skill_id
    if target.exists():
        raise SkillError(f'技能 {skill_id} 已存在')
    target.mkdir(parents=True)
    wrote = False
    for relative, payload in (files or {}).items():
        path = _safe_target(target, relative)
        if path is None:
            continue
        if path.name == _SKILL_FILE:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        wrote = True
    (target / _SKILL_FILE).write_text(text, encoding='utf-8')
    if package:
        _write_mark(target, digest=str(package.get('digest') or ''),
                    version=str(package.get('version') or ''),
                    filename=str(package.get('filename') or ''),
                    files=list(files or {}))
    state = _state()
    state[skill_id] = {'enabled': True, 'source': source}
    _write_state(state)
    row = get_skill(skill_id)
    if row is None:
        raise SkillError('写入后读不回技能元信息，请检查 SKILL.md')
    row['written_files'] = wrote
    return row


def sync_installed(filename: str, payload: bytes, *, skill_id: str,
                   digest: str = '', version: str = '',
                   fallback_description: str = '') -> dict | None:
    """把一份包重新解到**已经装过**的技能目录；没装过返回 `None`。

    为什么需要这个：安装是一次性的（`_write_skill` 见目录就报"已存在"），管理端
    换了包之后用户那份永远停在旧文件上——详情页"技能包内容 · 1 个文件"就是这么
    来的：当年没有包可用，先按元数据生成了一份 SKILL.md；后来补传的
    `references/`、`scripts/` 从来没有落地过，也没有任何路径会把它们补上去。

    边界：
    - 只动包里的文件；`.state.json` 里的启停状态、目录里用户自己加的文件都不碰；
    - 上一版有、这一版没有的文件按旧指纹清掉，避免换包后残留旧脚本被模型读到；
    - 默认安装**不会**因为这里而凭空建目录：目录不存在就是"没人装过"，返回 None。
    """
    if not valid_id(skill_id):
        raise SkillError(f'技能 id 不合法：{skill_id!r}（小写字母数字 _ . -，1~64 位）')
    directory = root() / skill_id
    if not directory.is_dir() or directory.is_symlink():
        return None
    members = _members_from_archive(filename, payload)
    prefix = _find_skill_root(members)
    try:
        raw = members[prefix + _SKILL_FILE].decode('utf-8')
    except UnicodeDecodeError as exc:
        raise SkillError('SKILL.md 必须是 UTF-8 文本') from exc
    meta, _ = parse_frontmatter(raw)
    if not str(meta.get('description') or '').strip() and fallback_description.strip():
        raw = _inject_description(raw, fallback_description.strip()[:240])
    files = _package_files(members, prefix)
    previous = _read_mark(directory)
    removed: list[str] = []
    for relative in previous.get('files') or []:
        text = str(relative or '')
        if not text or text in files:
            continue
        target = _safe_target(directory, text)
        if target is None or not target.is_file() or target.is_symlink():
            continue
        try:
            target.unlink()
            removed.append(text)
        except OSError:                                # pragma: no cover - 权限异常
            pass
    for relative, data in files.items():
        target = _safe_target(directory, relative)
        if target is None or target.name == _SKILL_FILE:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    # SKILL.md 最后写：中途失败时留下的是"旧说明 + 新附件"，比"新说明 + 缺附件"
    # 更容易在详情页上被看出来。
    (directory / _SKILL_FILE).write_text(raw, encoding='utf-8')
    final_digest = str(digest or '') or package_digest(payload)
    _write_mark(directory, digest=final_digest, version=version,
                filename=filename, files=list(files))
    row = get_skill(skill_id) or {}
    return {
        'skill_id': skill_id,
        'dir': str(directory),
        'files': len(files) + 1,
        'attachments': len(files),
        'removed': removed,
        'digest': final_digest,
        'file_tree': row.get('file_tree') or [],
    }


def _safe_target(base: Path, relative: str) -> Path | None:
    """包内相对路径 → 落盘路径；越界/绝对/反斜杠一律拒绝。"""
    text = str(relative or '').replace('\\', '/').strip()
    if not text or text.startswith('/') or re.match(r'^[A-Za-z]:', text):
        raise SkillError(f'包内路径不合法：{relative!r}')
    parts = [part for part in text.split('/') if part not in ('', '.')]
    if not parts or any(part == '..' for part in parts):
        raise SkillError(f'包内路径不允许穿越：{relative!r}')
    return base.joinpath(*parts)


def skill_md_from_catalog(item: dict) -> str:
    """目录里的技能（只有元数据）→ 一份可用的 SKILL.md。

    内置/市场目录的种子条目没有正文，直接"安装"会得到一个空技能。这里按条目
    的定位生成最小正文：描述 + 何时触发 + 用法要点。真正复杂的技能走上传/导入。
    """
    name = str(item.get('name') or item.get('id') or '技能').strip()
    description = str(item.get('description') or '').strip()
    category = str(item.get('category') or '').strip()
    overview = str(item.get('overview') or '').strip()
    prompt = str(item.get('prompt') or '').strip()
    examples = [str(x).strip() for x in (item.get('examples') or []) if str(x).strip()][:8]
    lines = [
        '---',
        f'name: {name}',
        f'description: {description}',
        f"version: {str(item.get('version') or '1.0.0')}",
        f"source: {str(item.get('source') or 'skillhub')}",
    ]
    if category:
        lines.append(f'category: {category}')
    if item.get('author'):
        lines.append(f"author: {item['author']}")
    lines += ['---', '', f'# {name}', '', description, '']
    if overview:
        lines += ['## 概述', '', overview, '']
    # 示例问法也写进 SKILL.md：用户在详情页点的那句话，和模型看到的口径应该一致。
    if examples:
        lines += ['## 试试这样用', ''] + [f'- {x}' for x in examples] + ['']
    lines += ['## 何时使用', '']
    if category:
        lines.append(f'当用户的需求属于「{category}」，或明确提到「{name}」时使用本技能。')
    else:
        lines.append(f'当用户明确提到「{name}」或需求与它的描述一致时使用本技能。')
    lines += ['', '## 用法', '', (prompt or description or '按技能描述完成用户的要求。'), '',
              '## 产出要求', '',
              '- 结论先行，再给依据；引用外部来源时保留链接。',
              '- 工具调用失败时如实说明失败原因，不要编造调用结果。']
    return '\n'.join(lines) + '\n'


def install_from_catalog(item: dict) -> dict:
    """把技能中心目录里的一条"安装"成磁盘技能（幂等）。"""
    skill_id = slug(str(item.get('id') or ''), fallback='')
    if not skill_id:
        raise SkillError('目录条目缺少合法 id')
    existing = get_skill(skill_id)
    if existing is not None:
        return existing
    return _write_skill(skill_id, skill_md_from_catalog(item),
                        source=str(item.get('source') or 'skillhub'))


# ---------------------------------------------------------------- 上传 / 导入


def _members_from_archive(filename: str, payload: bytes) -> dict[str, bytes]:
    """从 zip/tar.gz/单个 .md 里读出 `相对路径 -> 内容`，并做安全与体积校验。"""
    name = str(filename or '').lower()
    archive_limit = max_archive_bytes()
    member_limit = max_member_bytes()
    members_limit = max_members()
    if archive_limit and len(payload) > archive_limit:
        raise SkillError(f'技能包超过 {archive_limit // (1024 * 1024)} MB 上限')
    members: dict[str, bytes] = {}
    if name.endswith('.md'):
        members[_SKILL_FILE] = payload
    elif name.endswith('.zip'):
        try:
            archive = zipfile.ZipFile(io.BytesIO(payload))
        except zipfile.BadZipFile as exc:
            raise SkillError('不是合法的 zip 包') from exc
        with archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if member_limit and info.file_size > member_limit:
                    raise SkillError(
                        f'{info.filename} 超过单文件 {member_limit // (1024 * 1024)} MB 上限')
                members[info.filename] = archive.read(info)
                if len(members) > members_limit:
                    raise SkillError('技能包内文件过多')
    elif name.endswith(('.tar.gz', '.tgz', '.tar')):
        mode = 'r:gz' if name.endswith(('.tar.gz', '.tgz')) else 'r'
        try:
            archive = tarfile.open(fileobj=io.BytesIO(payload), mode=mode)
        except tarfile.TarError as exc:
            raise SkillError('不是合法的 tar 包') from exc
        with archive:
            for member in archive.getmembers():
                if member.isdir():
                    continue
                # 符号链接/硬链接会让解包变成"读到包外任意文件"，一律拒绝。
                if not member.isfile():
                    raise SkillError(f'技能包里不允许链接/设备文件：{member.name}')
                if member_limit and member.size > member_limit:
                    raise SkillError(
                        f'{member.name} 超过单文件 {member_limit // (1024 * 1024)} MB 上限')
                handle = archive.extractfile(member)
                members[member.name] = handle.read() if handle else b''
                if len(members) > members_limit:
                    raise SkillError('技能包内文件过多')
    else:
        raise SkillError('只支持 .zip / .tar.gz / .tgz / .md')

    clean: dict[str, bytes] = {}
    for relative, data in members.items():
        path = PurePosixPath(str(relative).replace('\\', '/'))
        if path.is_absolute() or any(part == '..' for part in path.parts):
            raise SkillError(f'包内路径不允许穿越：{relative!r}')
        text = '/'.join(part for part in path.parts if part not in ('', '.'))
        if text:
            clean[text] = data
    return clean


def _find_skill_root(members: dict[str, bytes]) -> str:
    """找包内技能根（含 SKILL.md 的最浅目录）；多候选视为歧义。"""
    candidates = [path for path in members
                  if path.rsplit('/', 1)[-1].lower() == _SKILL_FILE.lower()]
    if not candidates:
        raise SkillError('技能包里没有 SKILL.md')
    candidates.sort(key=lambda path: path.count('/'))
    depth = candidates[0].count('/')
    shallow = [path for path in candidates if path.count('/') == depth]
    if len(shallow) > 1:
        raise SkillError('技能包里有多个 SKILL.md，无法确定哪个是技能根')
    prefix = shallow[0].rsplit('/', 1)[0] if '/' in shallow[0] else ''
    return prefix + '/' if prefix else ''


def _package_files(members: dict[str, bytes], prefix: str) -> dict[str, bytes]:
    """包内成员 → 技能目录里的相对文件。

    只收**技能根下面**的文件。以前这里无条件 `path[len(prefix):]`：包的技能根
    是 `word/` 时，包根上的 `README.md` 会被切成 `.md` 落进技能目录，还可能顶掉
    根里的同名文件——那不是"多带几个文件"，是制造鬼文件。
    """
    files: dict[str, bytes] = {}
    for path, data in members.items():
        relative = _relative_member(path, prefix)
        if relative is None or relative == _SKILL_FILE:
            continue
        files[relative] = data
    return files


def _relative_member(path: str, prefix: str) -> str | None:
    """包内路径 → 相对技能根的路径；不在技能根内、或是垃圾文件就返回 None。

    `__MACOSX/`、`.DS_Store` 这类按**路径段**过滤：macOS 打的包处处都有它们，
    落进技能目录只会让模型读一堆二进制垃圾。
    """
    if prefix:
        if not path.startswith(prefix):
            return None
        path = path[len(prefix):]
    if not path:
        return None
    if any(part in _IGNORE or part == '__MACOSX' for part in path.split('/')):
        return None
    return path


# 控制台里点开一个文件看的是"这段写得对不对"，不是下载附件：超过这个字节数
# 只给前一段，免得一个 3 MB 的 json 把抽屉卡住。
PREVIEW_MAX_BYTES = 256 * 1024


def _looks_binary(data: bytes) -> bool:
    """NUL 字节是可靠的二进制信号；不做编码猜测，猜错会给出半屏乱码。"""
    return b'\x00' in data[:4096]


def archive_entries(filename: str, payload: bytes) -> dict:
    """包的目录清单（相对技能根），**完全在内存里做**，不落盘。

    给控制台的「查看包内容」用：上传完只看一行"已存 5 个文件"没法核对，
    人得能看见 references/、scripts/ 到底在不在。
    """
    members = _members_from_archive(filename, payload)
    prefix = _find_skill_root(members)
    entries = []
    for path, data in members.items():
        relative = _relative_member(path, prefix)
        if relative is None:
            continue
        entries.append({'path': relative, 'bytes': len(data),
                        'binary': _looks_binary(data)})
    entries.sort(key=lambda item: item['path'])
    return {'root': prefix.rstrip('/'), 'entries': entries}


def archive_member(filename: str, payload: bytes, relative: str) -> dict:
    """读包内单个文件的内容，用于预览。二进制文件只报体积，不回内容。"""
    raw = str(relative or '').replace('\\', '/').strip()
    target_path = PurePosixPath(raw)
    if not raw or target_path.is_absolute() or '..' in target_path.parts:
        raise SkillError('文件路径不合法')
    members = _members_from_archive(filename, payload)
    prefix = _find_skill_root(members)
    key = f'{prefix}{raw}'
    if key not in members:
        raise SkillError('包里没有这个文件')
    data = members[key]
    binary = _looks_binary(data)
    return {
        'path': raw,
        'bytes': len(data),
        'binary': binary,
        'truncated': len(data) > PREVIEW_MAX_BYTES,
        'text': '' if binary else data[:PREVIEW_MAX_BYTES].decode('utf-8', errors='replace'),
    }


def install_archive(filename: str, payload: bytes, *, skill_id: str = '',
                    fallback_description: str = '') -> dict:
    """上传技能包：校验 SKILL.md + frontmatter → 落到 `<skills_dir>/<id>/`。

    `fallback_description` 只给"目录条目的技能包"用：SkillHub 生成的 zip 里
    SKILL.md 可能没有 frontmatter（只有正文），而描述又是列表里唯一进上下文的
    元信息——用条目描述补一条，比整包拒绝或丢掉正文都合理。
    """
    members = _members_from_archive(filename, payload)
    prefix = _find_skill_root(members)
    skill_md = prefix + _SKILL_FILE
    try:
        raw = members[skill_md].decode('utf-8')
    except UnicodeDecodeError as exc:
        raise SkillError('SKILL.md 必须是 UTF-8 文本') from exc
    meta, _ = parse_frontmatter(raw)
    if not str(meta.get('description') or '').strip() and fallback_description.strip():
        raw = _inject_description(raw, fallback_description.strip()[:240])
        meta, _ = parse_frontmatter(raw)
    if not str(meta.get('description') or '').strip():
        raise SkillError('SKILL.md 的 frontmatter 缺少 description')
    derived = (slug(skill_id, fallback='')
               or slug(str(meta.get('name') or ''), fallback='')
               or slug(Path(str(filename)).stem, fallback='')
               or f'skill-{int(time.time()) % 100000}')
    return _write_skill(derived, raw, source='user',
                        files=_package_files(members, prefix),
                        name=str(meta.get('name') or '').strip(),
                        package={'digest': package_digest(payload),
                                 'version': str(meta.get('version') or ''),
                                 'filename': str(filename or '')})


def inspect_archive(filename: str, payload: bytes, *,
                    fallback_description: str = '') -> dict:
    """只校验、不落盘：给「先存包、用户点安装才解包」的入口用。

    与 `install_archive` 共用同一套解析，所以这里放过的包在安装时不会再因为
    结构问题失败——否则管理端显示"上传成功"、用户点「+」才报错，最难解释。
    """
    members = _members_from_archive(filename, payload)
    prefix = _find_skill_root(members)
    skill_md = prefix + _SKILL_FILE
    try:
        raw = members[skill_md].decode('utf-8')
    except UnicodeDecodeError as exc:
        raise SkillError('SKILL.md 必须是 UTF-8 文本') from exc
    meta, _ = parse_frontmatter(raw)
    description = (str(meta.get('description') or '').strip()
                   or fallback_description.strip())
    if not description:
        raise SkillError('SKILL.md 的 frontmatter 缺少 description')
    files = _package_files(members, prefix)
    return {
        'skill_md': skill_md,
        'root': prefix.rstrip('/'),
        'name': str(meta.get('name') or '').strip(),
        'description': description[:240],
        'version': str(meta.get('version') or '1.0.0'),
        'files': sorted(files),
        'file_count': len(files),
        'bytes': len(payload),
    }


def import_from_path(source: str, *, skill_id: str = '') -> dict:
    """把一个本地目录变成技能（WorkBuddy 的「从路径导入」）。

    目录可以没有 frontmatter 的 description——那就用名字补一条，否则导入完
    在列表里只剩一个 id，用户看不出它是什么。
    """
    path = Path(str(source or '')).expanduser()
    if not path.is_dir() or path.is_symlink():
        raise SkillError('路径不是目录')
    skill_md = path / _SKILL_FILE
    if not skill_md.is_file():
        raise SkillError(f'目录里没有 {_SKILL_FILE}')
    raw = skill_md.read_text(encoding='utf-8')
    meta, _ = parse_frontmatter(raw)
    if not str(meta.get('description') or '').strip():
        raw = _inject_description(raw, f'从本地目录导入的技能：{path.name}')
        meta, _ = parse_frontmatter(raw)
    derived = (slug(skill_id, fallback='') or slug(str(meta.get('name') or ''), fallback='')
               or slug(path.name, fallback=''))
    files: dict[str, bytes] = {}
    member_limit = max_member_bytes()
    for item in path.rglob('*'):
        if not item.is_file() or item.is_symlink():
            continue
        relative = item.relative_to(path)
        if any(part in _IGNORE for part in relative.parts):
            continue
        if relative.as_posix() == _SKILL_FILE:
            continue
        if member_limit and item.stat().st_size > member_limit:
            raise SkillError(
                f'{relative.as_posix()} 超过单文件 {member_limit // (1024 * 1024)} MB 上限')
        files[relative.as_posix()] = item.read_bytes()
    return _write_skill(derived, raw, source='user', files=files,
                        name=str(meta.get('name') or '').strip())


def _inject_description(text: str, description: str) -> str:
    """在 frontmatter 里补一条 description，没有 frontmatter 就新建。"""
    raw = str(text or '')
    if not raw.startswith('---'):
        return f'---\ndescription: {description}\n---\n\n{raw}'
    lines = raw.splitlines()
    out = [lines[0]]
    inserted = False
    for line in lines[1:]:
        if line.strip() in ('---', '...') and not inserted:
            out.append(f'description: {description}')
            inserted = True
        out.append(line)
    return '\n'.join(out) + '\n'


# ---------------------------------------------------------------- 启停 / 删除


def set_enabled(skill_id: str, enabled: bool) -> dict:
    row = get_skill(skill_id)
    if row is None:
        raise SkillError('技能不存在')
    state = _state()
    entry = state.get(skill_id) if isinstance(state.get(skill_id), dict) else {}
    state[skill_id] = {**entry, 'enabled': bool(enabled),
                       'source': entry.get('source') or row.get('source') or 'user'}
    _write_state(state)
    row['enabled'] = bool(enabled)
    return row


def remove_skill(skill_id: str) -> bool:
    """删技能目录。软禁用请走 `set_enabled(False)`。"""
    if not valid_id(skill_id):
        return False
    directory = root() / skill_id
    if not directory.is_dir() or directory.is_symlink():
        return False
    shutil.rmtree(directory, ignore_errors=True)
    state = _state()
    if skill_id in state:
        state.pop(skill_id, None)
        _write_state(state)
    return not directory.exists()


# ---------------------------------------------------------------- 会话物化


def session_dir(workspace_dir: str, session_id: str) -> tuple[Path, str]:
    """会话技能目录：返回 (绝对路径, 相对工作目录的路径)。

    放在工作目录里的隐藏目录，是为了让模型用现有的 `list_dir` / `read_file`
    按相对路径读到——不新增宿主工具，也不给 `read_file` 开第二个根。
    每轮整体重建，所以技能被移出会话后不会残留。
    """
    from webpage_agent.core import sandbox

    name = str(settings.skills_workspace_dirname or '.agent-skills')
    name = name.replace('\\', '/').strip().strip('/')
    # 只允许一层普通目录名：带 `..` 或多层的配置会让物化跑到工作目录外面。
    if not name or '/' in name or name in ('.', '..'):
        name = '.agent-skills'
    bucket = sandbox.segment(session_id)
    base = Path(workspace_dir) / name / bucket
    return base, f'{name}/{bucket}'


def materialize(skill_ids, *, workspace_dir: str, session_id: str) -> dict:
    """把本轮选中的技能拷进会话目录，返回可用清单与缺失 id。"""
    base, relative = session_dir(workspace_dir, session_id)
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    available, missing = [], []
    seen: set[str] = set()
    for raw in skill_ids or ():
        skill_id = str(raw or '').strip()
        if not skill_id or skill_id in seen:
            continue
        seen.add(skill_id)
        row = get_skill(skill_id)
        if row is None or not row.get('enabled', True):
            missing.append(skill_id)
            continue
        source = Path(row['root_path'])
        target = base / skill_id
        try:
            shutil.copytree(source, target,
                            ignore=shutil.ignore_patterns(*_IGNORE, _STATE_FILE, '.disabled'))
        except OSError:
            missing.append(skill_id)
            continue
        available.append({**row, 'dir': f'{relative}/{skill_id}',
                          'skill_md': f'{relative}/{skill_id}/{_SKILL_FILE}'})
    return {'dir': relative, 'path': str(base), 'skills': available,
            'missing': missing}


def render_prompt(materialized: dict, message_skill_ids: list[str] | None = None) -> str:
    """把物化结果渲染成系统提示里的一小段索引（不是全文）。

    `message_skill_ids` 是"消息级技能胶囊"点名的技能（`@skill:<id>` / `skill://<id>`），
    强度高于会话级选择：这些技能必须优先按正文执行，而不是"可用但可忽略"。
    """
    rows = list((materialized or {}).get('skills') or [])
    missing = [str(x) for x in ((materialized or {}).get('missing') or []) if str(x)]
    if not rows and missing:
        return ('\n\n【本轮技能】用户勾选了 ' + '、'.join(missing)
                + '，但这些技能没有安装或已被禁用，本轮不会加载任何技能正文。'
                '请如实告诉用户这些技能不可用，不要假装正在使用它们。')
    if not rows:
        return ('\n\n【本轮技能】用户没有勾选技能，也没有专家预设。'
                '按你自身的通用能力完成任务；不要声称正在使用某个技能。')
    lines = ['\n\n【本轮技能】用户在本轮勾选了以下技能。正文**没有**预加载，按需读取：']
    for row in rows:
        directory = str(row.get('dir') or '').strip()
        skill_md = str(row.get('skill_md') or '').strip()
        lines.append(f"- {row.get('name') or row['id']}（id={row['id']}）："
                     f"{str(row.get('description') or '').strip()[:200]}")
        if directory:
            lines.append(f"  目录：{directory}/；先 read_file(\"{skill_md}\")，"
                         f"references/、scripts/ 里的文件用到再读。"
                         f"读不到就 list_dir(\"{directory}/\") 核对真实路径，不要靠猜。")
    # 消息级点名：这些是用户在这条消息里用「技能胶囊」显式 @/命令 指定的，
    # 必须当成本轮主任务处理，而不是"顺手可用的技能"。
    named = [str(x) for x in (message_skill_ids or []) if str(x)]
    loaded = {str(row.get('id') or '') for row in rows}
    if named:
        hit = [skill_id for skill_id in named if skill_id in loaded]
        miss = [skill_id for skill_id in named if skill_id not in loaded]
        if hit:
            lines.append('\n用户在这条消息里通过「技能胶囊」明确点名了：'
                         + '、'.join(hit)
                         + '。优先按这些技能的正文执行；它们与上面索引里的同 id 对应。')
        if miss:
            lines.append('\n用户点名了 ' + '、'.join(miss)
                         + '，但它们没有安装或已被禁用，无法加载正文。'
                         '如实告知用户，不要假装正在使用。')
    lines += [
        '',
        '使用规则：',
        '1. 与用户请求相关的技能，先读它的 SKILL.md 再动手；不要凭技能名字猜步骤。',
        '2. 技能正文里的步骤优先级高于你的默认习惯，但不能越过工具白名单、'
        '连接器选择与高风险操作的执行前确认。',
        '3. 技能里的脚本可以直接读出来照做；执行脚本仍走本轮的既有工具能力。',
        '4. 用户没勾的技能不要假装在用；读不到某个文件时如实说明，不要编造内容。',
    ]
    return '\n'.join(lines)
