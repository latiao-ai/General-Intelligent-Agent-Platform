# 聊天回复的独立事件通道（SSE + 游标）

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
2. `timeline` 只返回最终 `answer`，没有中间事件；
3. 前端 `pollChatReply` 要等终态才刷新。

## 2. 现在的独立通道

### 2.1 事件表

```sql
session_turn_events (
  id         BIGSERIAL PRIMARY KEY,   -- 游标
  turn_id    TEXT NOT NULL,
  event_json TEXT NOT NULL,
  created_at TEXT NOT NULL
)
```

- `id` 自增，天然是游标；
- `turn_id` 索引；
- 一个 turn 一条独立事件流，不依赖任务 run 的 events 表。

### 2.2 写入

`_run_pi_chat` 给 Pi 传 `on_event`：

```python
async def on_event(event):
    if event["type"] == "answer_delta":
        await store.append_turn_event(turn_id, event)
        ...
state = await runtime.run(state, on_event=on_event)
```

- 每个 `answer_delta` 都落一条事件；
- 同时继续按 120ms 节流更新 `draft_answer`，作为不支持 SSE 时的兜底；
- 终态由 `settle_turn` 落 answer/status。

### 2.3 SSE 接口

```text
GET /api/sessions/{session_id}/turns/{turn_id}/events?after=0
```

- 支持 `after` 查询参数；
- 支持浏览器自动带回的 `Last-Event-ID`；
- 服务端按 `id > cursor` 增量读取；
- 每 80ms 检查一次新事件；
- 终态发：

```text
event: done
data: {"type":"done","status":"succeeded","answer":"..."}
```

示例：

```text
id: 1
event: turn
data: {"type":"answer_delta","text":"当前"}

id: 2
event: turn
data: {"type":"answer_delta","text":"工作目录下"}
...
event: done
data: {"type":"done","status":"succeeded","answer":"..."}
```

### 2.4 前端

- `streamChatTurn(turnId, sessionId)` 创建 `EventSource`；
- `turn` 事件里的 `answer_delta` 走现有 `onAnswerDelta()` 追加到气泡；
- `done` 事件关闭连接并 `openSession` 全量刷新；
- EventSource 断线重连会自动带 `Last-Event-ID`，不会重复/漏事件；
- SSE 不可用（404/代理不支持）时退化为原来的草稿轮询。

## 3. 实测

API 直接消费 SSE，看到 113 条 delta：

```text
delta id=1   len=2   '当前'
delta id=2   len=5   '工作目录下'
...
delta id=113 len=1   '）'
done status=succeeded answer_len=447
```

前端实测气泡文本长度逐步增长：

```text
17 → 89 → 139 → 181 → 219 → ... → 421
```

## 4. 边界

- 每条 delta 一次数据库写入；高频输出时数据库压力高于批量写；
- 模型首 token 之前仍然只能显示“正在思考”；
- SSE 端点每 80ms 轮询数据库，不是进程内广播；多副本部署也能用；
- `draft_answer` 仍保留，作为 SSE 失败时的兜底；
- 如果以后要跨设备恢复正在生成的回复，可以在打开会话时按 `last_event_id` 重新订阅。
