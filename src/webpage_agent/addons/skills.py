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

import io
import json
import re
import shutil
import tarfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from webpage_agent.core.config import settings

# 上传包的上限。技能是 prompt 包，不是数据集：20 MB 已经足够宽松，
# 单文件 5 MB 挡住"把模型/视频塞进技能"这类跑偏用法。
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_MEMBER_BYTES = 5 * 1024 * 1024
MAX_MEMBERS = 4000

_STATE_FILE = '.state.json'
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
        'root_path': str(directory),
        'files': files,
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


def skill_text(skill_id: str) -> str | None:
    row = get_skill(skill_id)
    if row is None:
        return None
    try:
        return (Path(row['root_path']) / _SKILL_FILE).read_text(encoding='utf-8')
    except OSError:
        return None


# ---------------------------------------------------------------- 安装


def _write_skill(skill_id: str, text: str, *, source: str, files: dict[str, bytes] | None = None,
                 name: str = '', description: str = '') -> dict:
    """把一份技能落到 `<skills_dir>/<id>/`。`files` 的键是相对技能根的路径。"""
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
    state = _state()
    state[skill_id] = {'enabled': True, 'source': source}
    _write_state(state)
    row = get_skill(skill_id)
    if row is None:
        raise SkillError('写入后读不回技能元信息，请检查 SKILL.md')
    row['written_files'] = wrote
    return row


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
    prompt = str(item.get('prompt') or '').strip()
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
    lines += ['---', '', f'# {name}', '', description, '',
              '## 何时使用', '']
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
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise SkillError(f'技能包超过 {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB 上限')
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
                if info.file_size > MAX_MEMBER_BYTES:
                    raise SkillError(f'{info.filename} 超过单文件上限')
                members[info.filename] = archive.read(info)
                if len(members) > MAX_MEMBERS:
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
                if member.size > MAX_MEMBER_BYTES:
                    raise SkillError(f'{member.name} 超过单文件上限')
                handle = archive.extractfile(member)
                members[member.name] = handle.read() if handle else b''
                if len(members) > MAX_MEMBERS:
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
    files = {path[len(prefix):]: data for path, data in members.items()}
    return _write_skill(derived, raw, source='user', files=files,
                        name=str(meta.get('name') or '').strip())


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
    for item in path.rglob('*'):
        if not item.is_file() or item.is_symlink():
            continue
        relative = item.relative_to(path)
        if any(part in _IGNORE for part in relative.parts):
            continue
        if relative.as_posix() == _SKILL_FILE:
            continue
        if item.stat().st_size > MAX_MEMBER_BYTES:
            raise SkillError(f'{relative.as_posix()} 超过单文件上限')
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


def render_prompt(materialized: dict) -> str:
    """把物化结果渲染成系统提示里的一小段索引（不是全文）。"""
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
                         f"references/、scripts/ 里的文件用到再读。")
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
