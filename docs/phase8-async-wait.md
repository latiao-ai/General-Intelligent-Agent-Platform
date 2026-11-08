# 第八阶段交付报告：远端异步任务持久等待与可靠唤醒

日期：2026-11-06。保留 LangGraph 生产默认引擎，不扩展通用平台写工具，不切换生产默认引擎。

## 自动测试

- 全量：**1077 项全部通过 + 92 子测试**（含真实 PostgreSQL `agent_platform_test`）。
- Node：**14 项全部通过**；`rm -rf dist && npm run build` 干净重建 + `node dist/main.js` 启动预检通过（不依赖本机残留 dist）。
- 命令：`PG_TEST_DSN=postgresql://…/agent_platform_test PYTHONPATH=src python -m pytest tests/ -q`。
- 受控模型 / 受控工具 / 受控远端替代真实供应商；真实 MCP / 供应商联调**另行报告**（需可查询任务身份与凭据）。

新增验收 `tests/test_phase8_async.py`（20 项）：

- **accepted 结算与外部任务登记原子提交**：动作 `accepted`（非 succeeded）+ external task + `waiting_external` 同一事务；唯一约束 `(run_id, action_id, item_key)` 防重复登记。
- **轮询领取/写回门禁**：旧 poller（epoch 不符 / 租约过期）写回无效；开放状态重查。
- **完成 + 唤醒同事务**：不复活 cancelled、不覆盖 paused、不抢占运行中执行者；重复完成无效。
- **Pi 持久等待协议**：`finished('waiting')` 合法；accepted 不注入 toolResult、不请求模型。
- **恢复先判断外部任务**：open→`resume_wait`、completed→`resume_deliver`、failed/cancelled→`resume_reconcile`。
- **空快照不被清洗成可执行默认值**。

## 十点实施摘要

1. **恢复基础一致性**：`_persist_snapshot` 租约校验+序号检查+写入同一事务（`save_snapshot_owned`）；`PiCallRegistry` 保存完整参数+工具绑定、结算不覆盖同轮候选、已交付不重复导出 pending；Hub 收尾提交真正版本化 Pi 快照（`_pi_snapshot`），空快照 `parse`/`parse_row` 抛 `SnapshotError` 不洗成默认值。
2. **首发异步合同**：`AsyncContract` 增 `idempotency_key_pointer`；`acceptance()` 三态（remote id / 幂等键 / 结果未知→needs_reconcile）；配置 `ASYNC_CONTRACTS_ENABLED`（`contract_id:version`）显式选择，不自动启用示例合同。
3. **accepted 语义**：`governed_call` 区分同步完成 / 远端受理 / 未知；`GovernedResult`/`ToolReceipt` 增 `accepted`/`remote_task_id`/`contract_id`；accepted 不转最终成功。
4. **原子提交**：`settle_action_accepted` + `register_external_task(conn=…)` 同事务保存回执、external task、调用关联、等待检查点、`waiting_external`；登记函数复用连接不提前 commit。
5. **持久等待协议**：协议增 `finished('waiting')` 与 `tool_result.accepted/remote_task_id`；宿主确认等待落库后 Node 结束本轮释放进程；快照 `awaiting_calls` 记录受理事实；恢复 `decide_external` 不套用 pending 重派发。
6. **轮询门禁**：`claim_external_task_poll` 重查开放状态/到期/租约；`finish_external_poll` 查 owner/epoch/有效期/状态迁移；查询限时 `external_poll_timeout` < 租约（续租兜底）；旧 poller/重复完成/失租不写回。
7. **结果可用分离**：结果物化失败进 `materializing`（有重试期限）；先稳定文件身份保存结果+manifest，再 DB 提交可消费引用。
8. **可靠唤醒**：`complete_external_task_and_wake` 结果与唤醒同事务（状态条件更新）；poller 不改 Pi messages，由宿主吸收结果注入 toolResult；不复活/覆盖/抢占。
9. **取消/超期/事件**：`request_external_cancel`/`confirm_external_cancel` 区分本地停止/远端取消请求/远端已确认取消；远端不支持取消如实保留状态；超期+未知→needs_reconcile；最终消息 dedupe 复用。
10. **故障注入**：见上，含状态词汇冻结清单更新（`waiting_external`）。

## 结构变更

- 迁移 `v7_external_cancel_intent`：`external_tasks.cancel_requested`。
- 状态词汇新增 `waiting_external`（`RUN_STATUSES` / `STOPPED_RUN_STATUSES`）。
