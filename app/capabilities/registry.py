"""能力包注册器：**机制在内核，声明归能力包**。

背景：以前加一个工具要改内核的 `app/tools.py` 两处（`BUILTIN_TOOLS` 列表 +
`_LOCAL_META` 身份表），再加一段 `CAPABILITY_RULES`——同一件事记在三张手工表里，
靠工具名对齐，改个名字就断。那是"内核知道业务"，与"通用中台"正好相反。

现在工具由**能力包自己**用 `@tool(...)` 声明，内核只负责汇总：

```python
# app/capabilities/notes.py  ← 加一个能力的全部内容就在这一个文件里
CAPABILITY = 'notes'
PROMPT_RULES = '【笔记】用户要记/查笔记时用它……'

@tool(name='save_note', label='记笔记', description='把一段文本存成本地笔记',
      args={'type': 'object', 'properties': {'text': {'type': 'string'}}},
      effect='write')
async def save_note(args, state):
    ...
```

要留意的两条边界：

1. **这里只注册本地能力**（进程内、无外部服务）。远端 MCP 工具是运行时发现
   出来的，有 TTL、掉线和前缀命名语义，由 `app/tools.py` 的 `mcp_tools()` 负责，
   两者最后在 `catalog()` 汇成一份清单——**不要**把 MCP 工具塞进这个注册表。
2. `PROMPT_RULES` 在**构建 Tool 时**才去模块上取，所以它写在文件哪个位置都行。
"""
import importlib
import logging
import pkgutil
import sys
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 动作性质白名单。没信息就是 unknown，不按名字猜（见文档 §5.2）。
VALID_EFFECTS = frozenset({'read', 'write', 'destructive', 'unknown'})


@dataclass(frozen=True)
class Tool:
    """一个可编排工具的描述。内核与能力包共用这一份定义。"""

    name: str
    label: str          # 中文阶段名，前端耗时表用它
    description: str    # 给规划器看：这个工具干什么
    args: dict          # 参数 JSON Schema，给规划器看
    run: object         # async def run(args, state) -> dict（state 增量）
    # 稳定身份：local:<能力包>:<动作>。模型只看 name，记录/审计/幂等键挂在 tool_key 上。
    tool_key: str = ''
    # 动作性质：read / write / destructive / unknown。
    effect: str = 'unknown'
    capability: str = ''
    # 规划规则：由**能力包自带**（模块级 PROMPT_RULES），规划器按候选工具所属能力拼接。
    prompt_rules: str = ''
    # 远端工具来自哪个服务端（本地工具为空）。多服务端时靠它做路由与排障。
    server_id: str = ''
    # public：答案在公开网页上（检索、抓页、把资料排成 HTML）。
    # 空：不是这条路。平台上的读和改不能拿 public 工具充数。
    scope: str = ''
    output_schema: dict | None = None
    annotations: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


_DECLARATIONS: dict[str, dict] = {}
_SPECS: dict[str, Tool] = {}
_VERSION = 0
_CACHED_VERSION = -1
_LOADED = False


def tool(*, name: str, label: str, description: str, args: dict,
         effect: str = 'unknown', key: str = '', capability: str = '',
         scope: str = ''):
    """把一个 async 函数登记成工具。

    只记录声明，真正的 `Tool` 在 `registered()` 里构建——这样能力包文件里
    `PROMPT_RULES` 的位置不影响注册，也避免了"模块还没执行完就去读自己的属性"。
    """
    def decorate(fn):
        global _VERSION
        _DECLARATIONS[name] = {'name': name, 'label': label,
                              'description': description, 'args': args,
                              'effect': effect, 'key': key,
                              'capability': capability, 'scope': scope,
                              'run': fn}
        _VERSION += 1
        return fn
    return decorate


def registered() -> list[Tool]:
    """当前已注册的本地工具，顺序稳定（按能力包名 + 工具名，便于测试与 diff）。"""
    global _CACHED_VERSION
    if _CACHED_VERSION != _VERSION:
        _SPECS.clear()
        for declaration in _DECLARATIONS.values():
            spec = _build(declaration)
            _SPECS[spec.name] = spec
        _CACHED_VERSION = _VERSION
    return sorted(_SPECS.values(), key=lambda item: (item.capability, item.name))


def load(force: bool = False) -> list[Tool]:
    """import 所有能力包（它们自己注册），返回工具清单。

    "扫描"本质是 import：包里有错会在启动时炸——这比运行时才发现好。
    import 幂等，重复调用不会重复注册。
    """
    global _LOADED
    if _LOADED and not force:
        return registered()
    package = importlib.import_module('app.capabilities')
    for info in pkgutil.iter_modules(package.__path__):
        # base/registry 是底座不是能力；下划线开头的是私有模块
        if info.name in {'base', 'registry'} or info.name.startswith('_'):
            continue
        importlib.import_module(f'app.capabilities.{info.name}')
    _LOADED = True
    return registered()


def snapshot() -> dict[str, Tool]:
    """按名字取表（内核用它做 `find_tool` 的第一跳）。"""
    return dict((spec.name, spec) for spec in registered())


def capability_modules() -> list:
    """已经加载的能力包模块。内核靠可选钩子找它们，不点名具体业务。"""
    load()
    package = importlib.import_module('app.capabilities')
    modules = []
    for info in pkgutil.iter_modules(package.__path__):
        if info.name in {'base', 'registry'} or info.name.startswith('_'):
            continue
        module = sys.modules.get(f'app.capabilities.{info.name}')
        if module is not None:
            modules.append(module)
    return modules


def planner_facts(state: dict, catalog) -> dict:
    """各能力包愿意告诉规划器的事实。包里的工具不在本轮清单里，就不出现。"""
    present = {getattr(tool, 'capability', '') for tool in catalog}
    facts = {}
    for module in capability_modules():
        capability = getattr(module, 'CAPABILITY', '')
        fn = getattr(module, 'planner_facts', None)
        if fn is None or not capability or capability not in present:
            continue
        extra = fn(state, catalog) or {}
        if isinstance(extra, dict):
            facts.update(extra)
    return facts


def suppressed_capabilities() -> set[str]:
    """开关关掉的能力。清单里整段拿掉，避免出现注定失败的工具。"""
    hidden = set()
    for module in capability_modules():
        capability = getattr(module, 'CAPABILITY', '')
        gate = getattr(module, 'enabled', None)
        if capability and gate is not None and not gate():
            hidden.add(capability)
    return hidden


def adapt_mcp_tool(server, item: dict, make_runner):
    """让能力包认领一个刚发现的远端工具。没有包认领就返回 None。"""
    for module in capability_modules():
        fn = getattr(module, 'adapt_mcp_tool', None)
        if fn is None:
            continue
        tool = fn(server, item, make_runner)
        if tool is not None:
            return tool
    return None


def mcp_placeholder(servers, errors: dict, *, respect_switch: bool = True):
    """发现失败时，由能力包自己决定要不要留一个会说清楚原因的占位工具。

    `respect_switch=False` 只给测试：直接构造占位，不看总开关。
    正常发现必须看开关，关掉的能力不留一个注定失败的工具。
    """
    for module in capability_modules():
        gate = getattr(module, 'enabled', None)
        if respect_switch and gate is not None and not gate():
            continue
        fn = getattr(module, 'mcp_placeholder', None)
        if fn is None:
            continue
        tool = fn(servers, errors)
        if tool is not None:
            return tool
    return None


def direct_tool(action: str) -> str:
    """按钮直接指定的工具名。哪个包响应这个动作，由包自己声明。"""
    for module in capability_modules():
        if getattr(module, 'DIRECT_ACTION', '') == action:
            return str(getattr(module, 'TOOL_NAME', '') or '')
    return ''


def _build(declaration: dict) -> Tool:
    fn = declaration['run']
    module = sys.modules.get(fn.__module__)
    capability = (declaration['capability']
                  or getattr(module, 'CAPABILITY', '')
                  or fn.__module__.rsplit('.', 1)[-1])
    spec = Tool(
        name=declaration['name'],
        label=declaration['label'],
        description=declaration['description'],
        args=declaration['args'],
        run=fn,
        tool_key=declaration['key'] or f'local:{capability}:{declaration["name"]}',
        effect=declaration['effect'],
        capability=capability,
        # 规则在构建时才取：能力包文件里 PROMPT_RULES 写在哪里都可以
        prompt_rules=getattr(module, 'PROMPT_RULES', ''),
        scope=declaration.get('scope') or '',
    )
    _check(spec)
    return spec


def _check(spec: Tool) -> None:
    """注册即校验：能力包长歪要在这里就失败，而不是等规划器拿到烂 schema。"""
    if spec.effect not in VALID_EFFECTS:
        raise ValueError(f'工具 {spec.name} 的 effect 非法：{spec.effect!r}')
    if not spec.label.strip() or not spec.description.strip():
        raise ValueError(f'工具 {spec.name} 缺少 label 或 description')
    if not isinstance(spec.args, dict) or spec.args.get('type') != 'object':
        raise ValueError(f'工具 {spec.name} 的 args 必须是 JSON Schema object')
    if not spec.tool_key.startswith('local:'):
        # 本地能力的身份必须自证是本地：远端工具的身份由 MCP 侧生成
        raise ValueError(f'工具 {spec.name} 的 tool_key 必须以 local: 开头')
    if spec.capability not in spec.tool_key:
        raise ValueError(f'工具 {spec.name} 的 capability 与 tool_key 不一致')
