"""用户画像：Markdown 文件 + 原子写 + 版本守卫。

借鉴 WorkBuddy 的「文件即记忆」：
- 画像是一份**人可读、可手改、可 git diff 的 Markdown**，而不是 SQLite 里的 JSON。
  它不是查询对象，是"每轮拼进 system prompt 的一小段文字"。
- 写入走「临时文件 + rename」原子替换，进程崩溃不会留下半截文件。
- 带 version 与 .bak 备份，便于手动回滚。

文件格式：

    # User Profile
    > Last updated: <ISO>
    > Version: <n>

    姓名：张三
    电话：138xxxx
    偏好：回答简洁、多用表格
"""
import os
import re
import uuid
from datetime import datetime, timezone

from app.config import settings

HEADER = '# User Profile'
_FACT_RE = re.compile(r'^([^#>:]+?)[:：](.+?)$')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _facts_of(text: str) -> dict:
    facts = {}
    for line in (text or '').splitlines():
        line = line.strip()
        if not line or line.startswith(('#', '>')):
            continue
        m = _FACT_RE.match(line)
        if m:
            facts[m.group(1).strip()] = m.group(2).strip()
    return facts


def _version_of(text: str) -> int:
    m = re.search(r'Version:\s*(\d+)', text or '')
    return int(m.group(1)) if m else 0


def load() -> dict:
    """读取画像事实；文件不存在时返回空。"""
    path = settings.profile_path
    if not path.exists():
        return {}
    return _facts_of(path.read_text(encoding='utf-8'))


def version() -> int:
    path = settings.profile_path
    if not path.exists():
        return 0
    return _version_of(path.read_text(encoding='utf-8'))


def render(facts: dict, ver: int) -> str:
    lines = [HEADER, f'> Last updated: {_now()}', f'> Version: {ver}', '']
    lines += [f'{key}：{value}' for key, value in facts.items()]
    lines.append('')
    return '\n'.join(lines)


def save(facts: dict) -> int:
    """原子写入，返回新版本号。"""
    path = settings.profile_path
    path.parent.mkdir(parents=True, exist_ok=True)
    new_ver = version() + 1
    if path.exists():
        try:
            (path.parent / (path.name + '.bak')).write_text(
                path.read_text(encoding='utf-8'), encoding='utf-8')
        except OSError:
            pass
    tmp = path.parent / f'.{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}'
    tmp.write_text(render(facts, new_ver), encoding='utf-8')
    os.replace(tmp, path)          # 原子替换，崩溃也不会留下半截
    return new_ver


def merge(updates: dict) -> dict:
    """把新事实合并进画像，返回**真正发生变化**的那些条目。

    只返回变化量，是为了让调用方能区分"学到了新东西"和"模型把已有事实又抄了一遍"。
    后者曾导致每轮都弹一次「已记住」——用户明明没说过那些话，界面却像刚记住。
    """
    if not updates:
        return {}
    facts = load()
    changed = {key: value for key, value in updates.items()
               if facts.get(key) != value}
    if not changed:
        return {}
    facts.update(changed)
    save(facts)
    return changed


def raw() -> str:
    """画像文件的原始文本，供界面展示与编辑。"""
    path = settings.profile_path
    return path.read_text(encoding='utf-8') if path.exists() else ''


def parse(text: str) -> dict:
    """把编辑框里的 Markdown 解析成事实。界面保存时与 load 走同一套口径。"""
    return _facts_of(text)


def clear() -> None:
    """清空画像。删之前先留一份 .bak——用户手一滑也能捞回来。"""
    path = settings.profile_path
    if not path.exists():
        return
    try:
        (path.parent / (path.name + '.bak')).write_text(
            path.read_text(encoding='utf-8'), encoding='utf-8')
    except OSError:
        pass
    path.unlink()


def block() -> str:
    """拼进 system prompt 的画像段；没有画像时返回空串。"""
    facts = load()
    if not facts:
        return ''
    return '[用户画像]\n' + '\n'.join(f'{k}：{v}' for k, v in facts.items())


def with_profile(system: str) -> str:
    """把画像作为背景拼进 system prompt（空画像原样返回）。"""
    b = block()
    if not b:
        return system
    return system + '\n\n' + b + '\n（以上是用户画像，仅作背景理解，不要逐字复述。）'


def describe(updates: dict) -> str:
    """把更新概括成给用户看的一句话。"""
    return '、'.join(f'{k}={v}' for k, v in updates.items())
