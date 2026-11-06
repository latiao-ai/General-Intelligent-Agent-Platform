# 第八阶段收口：异步任务端到端恢复与竞态修复

分支 `pi_agent_inset`。保留 LangGraph 生产默认、不接通用写工具、不扩展供应商。
10 点全部完成，Python 全量 1084 项通过（含真实 PG）+ 92 子测试，Node 14 项通过、
干净重建可启动。

## 十点实施摘要

1. **统一异步调用持久身份**：`awaiting_calls` 以 `external_task_id + action_id +
   logical_call_id` 为主关联，保存原 `call_id`（toolCallId）/ 工具键 / 合同版本；
   `remote_task_id` 仅远端查询身份。`decide_external` 改按主关联匹配，缺关联记录
   转 `RESUME_RECONCILE`（不再当「继续等待」）。`GovernedResult`/`ToolReceipt` 透传
   `external_task_id`。

2. **完整等待事实纳入 accepted 事务**：`governed_call` 先装配 `external` 再交给
   `build_snapshot(updates, tool_result, external)`，`awaiting_calls` 随
   `settle_action_accepted` 的同一事务写入；accepted 不写最终工具结果，不再依赖
   结算后第二次快照写补齐。`PI_CRASH_AFTER_ACCEPTED` 钩子支持提交后杀进程。

3. **外部结果真实交付路径**：`decide_external` 返回 `tool_results`（含
   last_result_id / output_manifest_ref）；`Hub._assemble_deliver_results` 读回
   标准结果存储登记的内容，装配成 `deliver_results`（原 toolCallId + 结果内容）；
   PiRuntime 经 `resume.deliver_results` 传给 Node，Node 直接 pushToolResult 后
   继续推理。吸收水位用「messages 是否已有该 toolCallId 的 toolResult」判断，
   重复恢复不重复追加；已有最终答复不被旧 awaiting 覆盖。

4. **修正恢复循环和多工具行为**：accepted 时 `build_snapshot` 先 `mark(accepted)`
   再导出 pending_calls，已受理调用不再作为待重派发候选残留；`decide_external`
   的 RESUME_DELIVER 同时带上剩余未派发的同步候选，Node 恢复时先注入结果再按原
   顺序处理剩余调用，不重新生成候选、不丢调用。

5. **外部终态与业务推进对应**：`complete_external_task_and_wake` 补齐
   failed/cancelled/needs_reconcile（→ needs_reconcile）；completed 唤醒同时匹配
   `waiting_external` 与「running 且租约已释放」；`finish_run_owned` 收尾为
   waiting_external 时同事务重查外部结果，完成先于等待收尾不永久等待；唤醒不覆盖
   暂停/取消/终态，不抢占运行中执行者。

6. **统一轮询写回四项门禁**：`finish_external_poll` / `complete_external_task_and_wake`
   / `confirm_external_cancel` 均检查 owner + epoch + 租约有效期 + 开放状态；迟到
   查询不得把已终态任务改回 running。补测试：租约过期但 epoch 未变、owner 不符、
   重复完成、确认取消后旧查询。

7. **等待状态取消生命周期**：`cancel_run` 接受 `waiting_external`，持久化取消意图，
   对开放外部任务发远端取消请求并查询确认；区分本地停止 / 远端取消请求 / 远端确认
   取消；不支持取消如实保留状态。completed 唤醒检查 `cancel_requested_at`，取消与
   完成竞争时取消意图优先。

8. **结果可消费并限制物化等待**：`_materialize` 改用 `stage_result` 产出标准
   staged，`complete_external_task_and_wake` 在 completed 时同一事务
   `commit_result_in_tx` 完成标准结果登记（不因缺 run 执行上下文跳过）。`mcp_adapter`
   `task_status` 取结果失败标记 `result_pending`，`poll_due` 进 materializing 重试
   不误判 succeeded。合同只有幂等键必须有 `idempotency_lookup_tool_key` 映射能力，
   否则不进正常等待。

9. **兼容默认 LangGraph 路径**：`nodes.py::run_step` 增加 accepted 分支：写
   tool_results（execution_status='accepted'）供观察识别 waiting_async，不写最终
   成功、不推进 step_index；`remember_waiting` 的 item_key 用 step_id 与
   `settle_action_accepted` 一致（幂等，不重复登记）。

10. **真实全链路验收**：新增 `tests/test_phase8_e2e.py`（真实 PG + 真实 Node +
    正式 Hub/Worker 入口 + 受控模型 `PI_MOCK_SCRIPT` + 受控异步远端
    `PI_MOCK_ASYNC` + 手动 querier）。完整走提交 → accepted → 等待 → 查询 → 保存
    → 唤醒 → 补入 → 回答 → 收尾，断言 succeeded、最终答复恰好一条、远端提交一次、
    答复使用指定远端结果。另有 accepted 后杀进程（`hub_async_crash_worker.py`）和
    多工具混合状态测试。修复 waiting_external 收尾误写最终答复占去重键的缺陷。

## 关键文件

- `src/webpage_agent/agent/pi_recovery.py`：decide_external 主关联匹配 + 交付事实
- `src/webpage_agent/agent/pi_runtime.py`：build_snapshot 收 external、deliver_results 装配
- `src/webpage_agent/agent/pi_host_tools.py`：ToolReceipt.external_task_id + 崩溃钩子
- `src/webpage_agent/agent/runtime.py`：PI_MOCK_ASYNC 受控异步工具
- `src/webpage_agent/tools/execution_gateway.py`：external 提前装配 + action_id 入 external
- `src/webpage_agent/store/pg_actions.py`：complete/finish/confirm 四项门禁 + 同事务结果登记
- `src/webpage_agent/core/external_tasks.py`：stage_result 物化 + result_pending + item_key 幂等
- `src/webpage_agent/core/async_contracts.py`：idempotency_lookup_tool_key
- `src/webpage_agent/api/hub_recovery.py`：_assemble_deliver_results + waiting_external 不写最终答复
- `src/webpage_agent/api/api_hub.py`：cancel_run 接受 waiting_external + _cancel_waiting_run
- `src/webpage_agent/agent/nodes.py`：run_step accepted 分支
- `src/webpage_agent/integrations/mcp_adapter.py`：task_status result_pending
- `pi-runtime/src/{protocol,main}.ts`：resume.deliver_results + finished('waiting')

## 边界

- 真实 MCP / 供应商联调未执行（需可查询任务身份与真实凭据），按规格另行报告；
  受控模型 + 受控远端已覆盖完整协议与恢复不变量。
- 生产默认引擎仍为 LangGraph；Pi 仅测试注入路径验证。
