"""Pi 真实联调验收脚本（第四阶段）。

独立于测试套件，用于手工验证「Python → Node 中的 Pi → Python 假工具 → Pi 答复」
这条链路。真实联调需要模型端点凭据（复用 .env 的 LLM_API_KEY / LLM_BASE_URL）。

与第三阶段 smoke 不同，本脚本是**有断言的验收脚本**：

    - 每条用例都校验结果，失败返回非零退出码；
    - 工具用例核实「Python 确实收到带正确参数的 tool_call 并回执」，而非只打印 42；
    - 缺少凭据时明确报错并返回非零退出码。

用法（在 main_branch/ 下）：

    PYTHONPATH=src python scripts/pi_smoke.py "你好"
    PYTHONPATH=src python scripts/pi_smoke.py "计算 17 加 25"

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
    """用例二：工具调用。核实 Python 收到带正确参数的 tool_call 并回执 42。"""
    tool_calls = []

    async def executor(name, args):
        tool_calls.append((name, args))
        if name == "add":
            return True, int(args.get("a")) + int(args.get("b"))
        return False, f"未知工具：{name}"

    runtime = PiRuntime(
        model={
            "id": settings.llm_model or "deepseek-v4-1-flash",
            "base_url": settings.llm_base_url or "https://www.hi-x.cn/v1",
            "provider": "deepseek",
            "api": "openai-completions",
        },
        api_key=settings.llm_key or None,
        system_prompt="你是一个严谨的助手。需要计算时请调用 add 工具。",
        tool_executor=executor,
    )
    state = {"task_id": "smoke-tool", "user_input": "计算 17 加 25", "history": []}
    result = await runtime.run(state)
    print("pi_outcome:", result.get("pi_outcome"))
    print("答复:", result.get("answer"))
    print("Python 侧收到的工具调用:", tool_calls)

    if result.get("pi_outcome") != "answered":
        return _fail(f"工具用例未成功，pi_outcome={result.get('pi_outcome')}")
    # 核实确实经过 Python 假工具（add 17 25 → 42）
    if not tool_calls:
        return _fail("未收到任何工具调用，说明没有经过 Python 假工具")
    names = [n for n, _ in tool_calls]
    if "add" not in names:
        return _fail(f"工具调用不是 add：{names}")
    add_args = [a for n, a in tool_calls if n == "add"]
    if not any(a.get("a") == 17 and a.get("b") == 25 for a in add_args):
        return _fail(f"add 参数不是 17/25：{add_args}")
    answer = str(result.get("answer", ""))
    if "42" not in answer:
        return _fail(f"最终答复未包含 42：{answer!r}")
    print("PASS: 工具调用经 Python 假工具，17+25=42")
    return 0


async def main() -> int:
    if not settings.llm_key:
        print("未配置 LLM_API_KEY，无法做真实联调。请在 .env 设置后重试。", file=sys.stderr)
        return 2

    args = sys.argv[1:]
    # 根据输入选择用例；默认两条都跑
    if args and args[0] == "你好":
        return await run_direct_answer()
    if args and "加" in args[0]:
        return await run_tool_answer()

    # 默认全跑
    rc = await run_direct_answer()
    if rc:
        return rc
    return await run_tool_answer()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
