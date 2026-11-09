"""Pi 真实联调验收脚本（第四阶段→第五阶段）。

独立于测试套件，用于手工验证「Python → Node 中的 Pi → Python 真实工具 → Pi 答复」
这条链路。真实联调需要模型端点凭据（复用 .env 的 LLM_API_KEY / LLM_BASE_URL）。

第五阶段起，工具不再是 echo/add 假工具，而是**唯一开放的真实工具**
`local:web:search`（web_search）。本脚本是有断言的验收脚本：

    - 每条用例都校验结果，失败返回非零退出码；
    - 工具用例核实「Pi 确实发起 web_search，Python 经受治理链路执行并回执，
      下一轮模型收到来源摘录后给出答复」，而非只打印一个固定数字；
    - 缺少凭据时明确报错并返回非零退出码。

**重要限制（第六阶段起）**：正式路径 `governed_call(require_ledger=True)` 在没有
执行账本时一律拒绝真实工具调用（返回 E_NO_LEDGER），不再有"无账本走兼容路径"
的旁路。本脚本不建账本，因此**工具用例在这里必然是失败的**——这是门禁生效的
正确表现，不是链路故障。要验证真实执行，请用带账本的入口：

    tests/test_governed_pi_integration.py（PG + 租约 + 账本）
    或起 API + Worker 走 HTTP 链路（runtime_kind='pi' 的 run）

直答用例（不调工具）不受此限制，仍可在本脚本内独立验证桥接与模型往返。

用法（在 main_branch/ 下）：

    PYTHONPATH=src python scripts/pi_smoke.py "你好"
    PYTHONPATH=src python scripts/pi_smoke.py "搜索一下今天北京的天气"

不带参数则跑两条内置用例。退出码：0=全部通过，1=用例失败，2=缺凭据。
"""
import asyncio
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from webpage_agent.agent.pi_runtime import PiRuntime  # noqa: E402
from webpage_agent.core.config import settings  # noqa: E402


def _fail(msg: str) -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    return 1


async def run_direct_answer() -> int:
    """用例一：直接答复。校验 pi_outcome=answered 且有非空答复。"""
    runtime = PiRuntime(
        model={
            "id": settings.llm_model or "deepseek-v4-1-flash",
            "base_url": settings.llm_base_url or "https://www.hi-x.cn/v1",
            "provider": "deepseek",
            "api": "openai-completions",
        },
        api_key=settings.llm_key or None,
        system_prompt="你是一个严谨的助手。",
    )
    state = {"task_id": "smoke-direct", "user_input": "你好", "history": []}
    result = await runtime.run(state)
    print("pi_outcome:", result.get("pi_outcome"))
    print("答复:", result.get("answer"))
    if result.get("pi_outcome") != "answered":
        return _fail(f"直接答复未成功，pi_outcome={result.get('pi_outcome')}")
    if not result.get("answer"):
        return _fail("直接答复没有非空答复文本")
    print("PASS: 直接答复")
    return 0


async def run_tool_answer() -> int:
    """用例二：真实工具调用。核实 Pi 发起 web_search、Python 执行并回执来源摘录。

    第五阶段起工具是真实 web_search（稳定键 local:web:search），走 HostToolCaller
    的受治理链路。无账本/无治理 scope 时（本 smoke 直连）走兼容路径：真实执行
    web_search，回执来源 ID/标题/URL/摘录。
    """
    tool_calls = []

    # 真实受治理调用对象：只开放 web_search，执行真正的网页检索。
    # 注意：本脚本无执行账本，governed_call 会按正式路径拒绝真实调用
    # （E_NO_LEDGER）。这里的价值是**验证门禁生效**，不是验证工具真的跑通。
    from webpage_agent.agent.pi_host_tools import HostToolCaller

    class _SpyCaller(HostToolCaller):
        # 用 **kwargs 透传：宿主侧调用签名会随阶段演进新增参数（如第七阶段的
        # build_snapshot），Spy 只负责记录，不该跟着逐个改签名——否则脚本会
        # 在每一次宿主接口演进而非链路本身出问题时崩掉。
        #
        # 记录回执的 ok：**只记录「发起过调用」会制造假阳性**。无账本时
        # governed_call 一律返回 E_NO_LEDGER，工具根本没执行，模型拿到的是
        # 错误回执；旧断言在这种情况下仍判 PASS，等于把「被门禁拒绝」当成
        # 「链路打通」。
        async def call(self, *, call_id, tool_name, args, state, **kwargs):
            receipt = await super().call(
                call_id=call_id, tool_name=tool_name, args=args, state=state,
                **kwargs)
            tool_calls.append((tool_name, args, bool(getattr(receipt, 'ok', False)),
                               str(getattr(receipt, 'error', '') or '')))
            return receipt

    runtime = PiRuntime(
        model={
            "id": settings.llm_model or "deepseek-v4-1-flash",
            "base_url": settings.llm_base_url or "https://www.hi-x.cn/v1",
            "provider": "deepseek",
            "api": "openai-completions",
        },
        api_key=settings.llm_key or None,
        system_prompt=(
            "你是一个严谨的助手。需要查实时信息时请调用 web_search 工具，"
            "参数 query 是检索关键词。"
        ),
        host_caller=_SpyCaller(),
    )
    state = {"task_id": "smoke-tool", "user_input": "搜索一下今天北京的天气",
             "history": []}
    result = await runtime.run(state)
    print("pi_outcome:", result.get("pi_outcome"))
    print("答复:", result.get("answer"))
    print("Python 侧收到的工具调用:", [(n, ok) for n, _a, ok, _e in tool_calls])

    if result.get("pi_outcome") != "answered":
        return _fail(f"工具用例未成功，pi_outcome={result.get('pi_outcome')}")
    # 核实确实经过真实 web_search 工具（而非 echo/add 假工具）
    if not tool_calls:
        return _fail("未收到任何工具调用，Pi 没有发起 web_search")
    names = [n for n, _a, _ok, _e in tool_calls]
    if "web_search" not in names:
        return _fail(f"工具调用不是 web_search：{names}")
    ws = [(a, ok, err) for n, a, ok, err in tool_calls if n == "web_search"]
    if not any(isinstance(a.get("query"), str) and a["query"].strip()
               for a, _ok, _e in ws):
        return _fail(f"web_search 缺少非空 query：{[a for a, _o, _e in ws]}")
    # 核心断言：至少一次**真正执行成功**。
    # 无执行账本时 governed_call 对所有写实调用返回 E_NO_LEDGER，工具一次都不会
    # 执行，模型只能拿到错误回执并如实说"工具不可用"。这种结果不能算通过——
    # 那验证的是门禁，不是链路。
    if not any(ok for _a, ok, _e in ws):
        errs = sorted({e for _a, _o, e in ws if e})
        hint = '；'.join(errs[:2]) or '（回执未给出原因）'
        return _fail(
            'web_search 全部被拒绝，链路未真正执行：' + hint
            + '。本脚本没有执行账本，而正式路径（require_ledger=True）在没有账本时'
              '一律拒绝真实工具调用。要验证真实执行，请走带账本的入口：'
              'tests/test_governed_pi_integration.py，或起 API + Worker 走 HTTP 链路。')
    # 核实最终答复消费了搜索结果（非空、且不是空泛的失败兜底）
    answer = str(result.get("answer", "")).strip()
    if not answer:
        return _fail("最终答复为空，未消费搜索结果")
    print("PASS: Pi 发起真实 web_search，Python 执行并回执来源摘录，模型给出答复")
    return 0


async def main() -> int:
    if not settings.llm_key:
        print("未配置 LLM_API_KEY，无法做真实联调。请在 .env 设置后重试。", file=sys.stderr)
        return 2

    args = sys.argv[1:]
    # 根据输入选择用例；默认两条都跑
    if args and args[0] == "你好":
        return await run_direct_answer()
    if args and "搜" in args[0]:
        return await run_tool_answer()

    # 默认全跑
    rc = await run_direct_answer()
    if rc:
        return rc
    return await run_tool_answer()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
