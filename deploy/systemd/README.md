# systemd 单元：把 API 和 Worker 变成"挂了自动起来"的服务

## 为什么是两个服务而不是一个

API 只做接纳（写库 + 202），**执行**是 Worker 的事。两者合在一个进程或一个
终端会话里，就会出现本项目真实踩过的坑：会话结束、进程被回收，API 还活着、
页面还能打开，但没有任何进程去领消息——消息永久停在 `accepted`，客户看到
"发出去没回复"。

分开之后还有一个好处：Worker 可以单独扩容/重启，不影响 API 的会话与历史读取。

## 安装

```sh
# 1. 账号与目录（一次性）
sudo useradd --system --home /opt/webpage-agent --shell /usr/sbin/nologin agent
sudo mkdir -p /var/lib/webpage-agent && sudo chown agent:agent /var/lib/webpage-agent
sudo mkdir -p /etc/webpage-agent && sudo chmod 750 /etc/webpage-agent

# 2. 配置（权限必须是 600/640，里面是模型与数据库密钥）
sudo install -m 640 -o root -g agent deploy/production.env.example /etc/webpage-agent/production.env
sudo editor /etc/webpage-agent/production.env

# 3. 单元
sudo install -m 644 deploy/systemd/webpage-agent-api.service /etc/systemd/system/
sudo install -m 644 deploy/systemd/webpage-agent-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now webpage-agent-api webpage-agent-worker
```

## 自检（每次部署后跑一遍）

```sh
systemctl status webpage-agent-api webpage-agent-worker --no-pager
journalctl -u webpage-agent-worker -n 20 --no-pager     # 应看到「启动 worker_id=…」
curl -s localhost:8000/api/health/live                  # {"ok":true}
```

执行器是否在干活，看这两个数（operator 身份访问）：

```sh
curl -s -b 'agent_session=…' localhost:8000/api/health | jq '{workers, turns}'
# 期望：workers.alive >= 1，turns.oldest_accepted_age 保持在个位数
```

`workers.alive == 0` 就是"页面能打开，但没人回复"的准确特征——监控直接对这条
告警（`/api/ops/metrics` 里叫 `worker_alive`）。

## 常见误配

- **只 `enable` 了 api**：Worker 不跑，所有聊天都 503（这是有意的：
  宁可当场报错，也不收下一条没人执行的消息）。
- **`REQUIRE_LIVE_WORKER=false` 当成"修复"**：那只是把 503 换回静默等待。
  真出问题时应该修 Worker，不是关掉门禁。
- **`ProtectHome=read-only` 与工作目录冲突**：如果代码或运行数据放在
  `/home` 下，`ReadWritePaths` 要跟着改，否则写库/写产物会 EROFS。
- **`TimeoutStopSec` 太小**：Worker 退出时要收尾 in-flight 的 turn，
  给到 30 秒，别让 systemd 直接 SIGKILL。
