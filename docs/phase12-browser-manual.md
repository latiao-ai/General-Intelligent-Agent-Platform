# 第十二阶段缺口修复：浏览器手工测试清单与记录

> 评审点 4：无 Playwright 也可手工测试浏览器行为。本文件是**手工测试清单 +
> 记录模板**，需在浏览器端操作后回填结果。

## 前置准备

1. 启动本地服务（API + Worker）：
   ```bash
   cd main_branch
   PYTHONPATH=src /opt/anaconda3/envs/demo/bin/python -m scripts.serve_api --port 8002
   PYTHONPATH=src /opt/anaconda3/envs/demo/bin/python -m scripts.run_worker
   ```
2. 浏览器打开 `http://127.0.0.1:8002`（或前端页面入口），登录/换 ticket。

## 测试项（逐项记录结果）

| # | 场景 | 操作步骤 | 期望 | 实际结果 | 结论 |
|---|---|---|---|---|---|
| 1 | 连续发消息 | 连发 3 条「你好」「今天天气怎么样」「再讲个笑话」 | 逐条按序返回答复，不串话、不丢消息 | | |
| 2 | 刷新页面 | 发一条消息后立即刷新浏览器 | 刷新后历史消息仍在，未答复的 turn 轮询恢复出结果 | | |
| 3 | 断网重连 | 发消息后断开网络 10s，再恢复 | 重连后 pending turn 经轮询补回结果，不重复答复 | | |
| 4 | 切换会话 | 在会话 A 发消息后切到会话 B 再发 | 两个会话的消息/timeline 互不串扰 | | |
| 5 | 后台任务并存 | 建一个业务任务（POST /api/tasks），同时继续聊天 | 聊天不被后台任务阻塞，任务进度可查 | | |

## 记录要点

- 每项记录：是否通过 / 现象 / 是否重复尝试 / 控制台报错（如有）。
- 断网重连重点看：前端 `pollChatReply` 是否按 `turn_id + sessionId` 幂等恢复，
  不重复展示同一条答复。
- 若某项失败，记录浏览器控制台报错 + API 日志，不要笼统写「失败」。

---

## 附：SIGKILL 复验结论（评审点 3）

评审原文称完整回归里那次 Worker 子进程失败为 **SIGILL**。经复核，实际记录是
**退出码 -9 = SIGKILL**（外部强杀），非 SIGILL（非法指令）。两者是不同的信号：
- SIGKILL（-9）：外部强制终止，通常是资源耗尽 / 超时清理 / 测试框架 kill；
- SIGILL（-4 或 132）：进程执行了非法指令，通常是程序 bug（越界跳转、损坏代码）。

**复验记录**：
- 失败用例：`test_worker_execution.WorkerExecutionTests.test_worker_process_starts_and_exits_cleanly`
- 路径：起真实 Worker 子进程 `--jobs-only --once`，只验证「启动对账 → 退出码 0」，
  **不经本轮改动的聊天/业务调度路径**。
- 相同条件复验 5 次：全部通过，退出码 0，单次 ~0.88s。
- 判定：该失败是完整 `unittest discover`（1157 项、多子进程测试累积）下的
  **偶发资源/时序问题**（子进程被外部 SIGKILL），非程序缺陷；单独复验稳定，
  且路径与本轮改动无关。**结论不再写「偶发所以忽略」，改为「已复验 + 归因」**。
