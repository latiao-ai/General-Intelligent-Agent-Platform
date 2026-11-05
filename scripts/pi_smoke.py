"""Pi 真实联调 smoke 入口（第三阶段）。

独立于测试套件，用于手工验证「Python → Node 中的 Pi → Python 假工具 → Pi 答复」
这条链路。真实联调需要模型端点凭据（复用 .env 的 LLM_API_KEY / LLM_BASE_URL）。

用法（在 main_branch/ 下）：

    PYTHONPATH=src python scripts/pi_smoke.py "你好"
    PYTHONPATH=src python scripts/pi_smoke.py "计算 17 加 25"

不带参数则跑两条内置用例（"你好"直接答复 + "17+25=42" 经假工具）。
"""
import asyncio
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from webpage_agent.agent.pi_runtime import PiRuntime  # noqa: E402
from webpage_agent.core.config import settings  # noqa: E402


async def run_one(text: str) -> None:
    runtime = PiRuntime(
        model={
            "id": settings.llm_model or "deepseek-v4-1-flash",
            "base_url": settings.llm_base_url or "https://www.hi-x.cn/v1",
            "provider": "deepseek",
            "api": "openai-completions",
        },
        api_key=settings.llm_key or None,
        system_prompt="你是一个严谨的助手。需要计算时请调用 add 工具。",
    )

    async def on_event(event):
        if event.get("type") == "answer_delta":
            print(event.get("delta", ""), end="", flush=True)

    state = {"task_id": "smoke", "user_input": text, "history": []}
    result = await runtime.run(state, on_event=on_event)
    print("\n---")
    print("状态:", result.get("status"))
    print("答复:", result.get("answer"))
    print("errors:", result.get("errors"))
    print("===")


async def main() -> None:
    if not settings.llm_key:
        print("未配置 LLM_API_KEY，无法做真实联调。请在 .env 设置后重试。", file=sys.stderr)
        sys.exit(2)

    args = sys.argv[1:]
    cases = args if args else ["你好", "计算 17 加 25"]

    for text in cases:
        print(f"\n>>> 输入: {text}")
        try:
            await run_one(text)
        except Exception as exc:  # noqa: BLE001
            print(f"联调失败: {type(exc).__name__}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
