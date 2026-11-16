"""消息平台（IM 网关）：把飞书 / 企业微信 / 个人微信接到本项目的会话执行链上。

模块边界：

- `models` / `base`：契约与 Transport ABC；
- `registry`：渠道清单（字段元数据、必填 env、能力声明）；
- `state`：由配置与运行态**派生**的展示状态（纯函数）；
- `router`：会话键、鉴权（默认拒绝）、命令；
- `bridge`：一条 IM 消息 → 一次会话 turn → 答复；
- `gateway`：渠道生命周期、入站队列、出站重试、单实例锁；
- `transports/*`：各平台协议适配器；
- `weixin_login`：个人微信扫码登录（API 进程里跑）。

设计说明见 `docs/messaging-platforms.md`。
"""
