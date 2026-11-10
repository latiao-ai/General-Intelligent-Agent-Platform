# 聊天回复为什么不是流式，以及现在的做法

## 1. 原来的原因

任务 run 有 SSE 事件流，所以 `answer_delta` 能边生成边显示；但会话聊天/查询 turn
是另一条链路：

```text
POST /api/sessions/{id}/messages
  → Worker 执行 turn
  → PiRuntime 生成 answer_delta
  → 只把最终 answer 落库
  → 前端每 800ms 轮询 /timeline
  → 只有 turn.status 到终态才 openSession 刷新
```

具体丢在三个地方：

1. `_run_pi_chat` 调 `runtime.run(state)` 时没传 `on_event`，Pi 的
   `answer_delta` 事件直接丢掉；
2. `timeline` 只返回 turn 的最终 `answer`，没有中间草稿；
3. 前端 `pollChatReply` 要等 `chatTurnDone` 才刷新，所以看起来像“整段直接跳出”。

## 2. 现在的实现

### 2.1 后端保存草稿

- `session_turns` 增加 `draft_answer`；
- `_run_pi_chat` 给 Pi 传 `on_event`，收到 `answer_delta` 后累积；
- 按 120ms 节流写入 `draft_answer`，不是每个 token 都打数据库；
- `timeline.chat_turns` 返回 `draft`。

### 2.2 前端边轮询边画

- `pollChatReply` 轮询间隔从 800ms 改为 280ms；
- 只要 `turn.draft` 有内容，就用同一个 assistant 气泡覆盖文本；
- 草稿出现后隐藏“正在思考”；
- 终态仍调用 `openSession` 做一次全量刷新，保证最终答复与数据库一致。

## 3. 实测

问一个约 400 字的介绍，轮询看到草稿持续增长：

```text
0.0s  draft_len=0
5.8s  draft_len=5
6.0s  draft_len=40
6.2s  draft_len=61
7.0s  draft_len=120
7.2s  draft_len=326
7.8s  draft_len=411
8.2s  draft_len=552
8.5s  finished, answer_len=591
```

## 4. 边界

- 这是“轮询草稿”，不是真正的 SSE token 流；延迟约 120–280ms；
- 模型首 token 之前仍然只能显示“正在思考”；
- 草稿落库是展示用途，不参与最终答案一致性；终态以 `settle_turn` 的 answer 为准；
- 如果以后要做真正的 SSE，需要为 session turn 增加独立事件通道/游标。
