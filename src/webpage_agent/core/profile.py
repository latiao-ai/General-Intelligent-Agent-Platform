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
import contextvars
import fcntl
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from webpage_agent.core.config import settings

HEADER = '# User Profile'
DEFAULT_USER = 'default'
_FACT_RE = re.compile(r'^([^#>:]+?)[:：](.+?)$')

# 面板编辑框里那行"更新时间"是展示用的，不是画像事实。
# 用户打开看一眼什么都没改就点保存时，它不能变成档案里的第 N 条事实——
# 所以解析时按这个键名剔掉（见 `_facts_of`）。
UPDATED_KEY = '更新时间'
_RESERVED_KEYS = {UPDATED_KEY}
# 当前操作主体。单用户时恒为 default；接上登录后由请求层/执行层绑定。
# 用 contextvar 而不是加参数，是因为画像在 nodes（拼 system prompt）与
# Hub（记忆合并）里到处都在用，逐层传参会污染整条调用链。
_CURRENT_USER: contextvars.ContextVar[str] = contextvars.ContextVar(
    'profile_user', default=DEFAULT_USER)


def use(user: str | None) -> contextvars.Token:
    """切换当前画像主体，返回可用于 reset 的 token。"""
    return _CURRENT_USER.set(user or DEFAULT_USER)


def reset(token: contextvars.Token) -> None:
    _CURRENT_USER.reset(token)


def _safe(user: str) -> str:
    """主体名进文件名前先净化，避免 `../` 之类的路径穿越。"""
    return re.sub(r'[^A-Za-z0-9_.-]', '_', user)[:64] or DEFAULT_USER


def _path(user: str | None = None) -> Path:
    """当前主体的画像文件。

    单用户（default）仍然用 settings.profile_path，保持既有文件与 git 习惯；
    多用户落到同级 `users/<user>.md`，互不覆盖（文档 §10：画像按用户作用域）。

    `user` 显式传入时不读 contextvar：聊天 turn 是独立协程，靠全局状态绑定
    主体只有在同一段 try/finally 里才安全，否则异常退出会把主体留在 context 里，
    下一条读画像的路径就可能读到别人的档案。
    """
    base = settings.profile_path
    name = str(user or '').strip() or _CURRENT_USER.get()
    if name == DEFAULT_USER:
        return base
    return Path(base).parent / 'users' / f'{_safe(name)}.md'


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
            key = m.group(1).strip()
            if key in _RESERVED_KEYS:
                continue
            facts[key] = m.group(2).strip()
    return facts


def _version_of(text: str) -> int:
    m = re.search(r'Version:\s*(\d+)', text or '')
    return int(m.group(1)) if m else 0


def load(user: str | None = None) -> dict:
    """读取画像事实；文件不存在时返回空。"""
    path = _path(user)
    if not path.exists():
        return {}
    return _facts_of(path.read_text(encoding='utf-8'))


def version(user: str | None = None) -> int:
    path = _path(user)
    if not path.exists():
        return 0
    return _version_of(path.read_text(encoding='utf-8'))


def render(facts: dict, ver: int) -> str:
    lines = [HEADER, f'> Last updated: {_now()}', f'> Version: {ver}', '']
    lines += [f'{key}：{value}' for key, value in facts.items()]
    lines.append('')
    return '\n'.join(lines)


class ProfileConflict(RuntimeError):
    """别的进程已经写过更新的版本。"""


def save(facts: dict, *, expected_version: int | None = None,
         user: str | None = None) -> int:
    """原子写入，返回新版本号。

    同一台机器上的多个进程用文件锁串行化。多台机器要共用同一个画像目录。
    带了 expected_version 时，版本已经变过就拒绝覆盖。
    """
    path = _path(user)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + '.lock')
    with open(lock_path, 'a', encoding='utf-8') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            current = version(user)
            if expected_version is not None and current != int(expected_version):
                raise ProfileConflict('画像版本已变化，请重新读取后再保存')
            new_ver = current + 1
            if path.exists():
                try:
                    (path.parent / (path.name + '.bak')).write_text(
                        path.read_text(encoding='utf-8'), encoding='utf-8')
                except OSError:
                    pass
            tmp = path.parent / f'.{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}'
            tmp.write_text(render(facts, new_ver), encoding='utf-8')
            os.replace(tmp, path)
            return new_ver
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def merge(updates: dict, user: str | None = None) -> dict:
    """把新事实合并进画像，返回**真正发生变化**的那些条目。

    只返回变化量，是为了让调用方能区分"学到了新东西"和"模型把已有事实又抄了一遍"。
    后者曾导致每轮都弹一次「已记住」——用户明明没说过那些话，界面却像刚记住。

    `user` 传了就给指定主体写（后台提取用），不传仍走 contextvar。
    """
    if not updates:
        return {}
    facts = load(user)
    changed = {key: value for key, value in updates.items()
               if facts.get(key) != value}
    if not changed:
        return {}
    facts.update(changed)
    save(facts, user=user)
    return changed


def raw() -> str:
    """画像文件的原始文本，供排障与导出；界面编辑框用 `editable()`。"""
    path = _path()
    return path.read_text(encoding='utf-8') if path.exists() else ''


def _human_time(text: str) -> str:
    """把文件头里的 ISO 时间换成本机时区的可读时间。

    `2026-11-16T02:44:37.213932+00:00` 摆在编辑框里，用户得先在脑子里做时区
    换算才看得懂"这是几点"。秒和微秒对这个场景没有意义，显示到分钟就够。
    """
    m = re.search(r'Last updated:\s*(\S+)', text or '')
    if not m:
        return ''
    try:
        stamp = datetime.fromisoformat(m.group(1))
    except ValueError:
        return ''
    return stamp.astimezone().strftime('%Y-%m-%d %H:%M')


def editable(user: str | None = None) -> str:
    """「我的画像」编辑框里的文本：一行更新时间 + 事实行。

    文件里那两行元信息各有各的用处，但都不是"我是谁"：

    - `# User Profile` 让 git diff 和外部工具认得出这是画像文件；
    - `> Version: n` 是并发写保护（`expected_version`），用户改不了也不关心。

    所以编辑框只留事实，时间换成人话摆第一行。保存时回到 `parse()`，
    那行时间会被 `_RESERVED_KEYS` 剔掉，不会混进档案变成一条伪事实。
    """
    path = _path(user)
    if not path.exists():
        return ''
    text = path.read_text(encoding='utf-8')
    lines = []
    stamp = _human_time(text)
    if stamp:
        lines += [f'{UPDATED_KEY}：{stamp}', '']
    lines += [f'{key}：{value}' for key, value in _facts_of(text).items()]
    return '\n'.join(lines) + '\n' if lines else ''


def parse(text: str) -> dict:
    """把编辑框里的 Markdown 解析成事实。界面保存时与 load 走同一套口径。"""
    return _facts_of(text)


def clear() -> None:
    """清空画像。删之前先留一份 .bak——用户手一滑也能捞回来。"""
    path = _path()
    if not path.exists():
        return
    try:
        (path.parent / (path.name + '.bak')).write_text(
            path.read_text(encoding='utf-8'), encoding='utf-8')
    except OSError:
        pass
    path.unlink()


def block(user: str | None = None) -> str:
    """拼进 system prompt 的画像段；没有画像时返回空串。"""
    facts = load(user)
    if not facts:
        return ''
    return '[用户画像]\n' + '\n'.join(f'{k}：{v}' for k, v in facts.items())


def with_profile(system: str, user: str | None = None) -> str:
    """把画像作为背景拼进 system prompt（空画像原样返回）。"""
    b = block(user)
    if not b:
        return system
    return system + '\n\n' + b + '\n（以上是用户画像，仅作背景理解，不要逐字复述。）'


def describe(updates: dict) -> str:
    """把更新概括成给用户看的一句话。"""
    return '、'.join(f'{k}={v}' for k, v in updates.items())
