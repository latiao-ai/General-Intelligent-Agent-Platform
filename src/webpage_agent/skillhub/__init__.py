"""技能中心（SkillHub）：技能/连接器的目录、安装状态与 API。

与 `capabilities/` 的区别：
- `capabilities/` 是**进程内工具**（能直接执行的函数）；
- `skillhub/` 是**面向用户的技能目录**（可安装、可展示、可下发给模型的工作方式），
  目录是静态的，安装状态按主体持久化到 `profile/skillhub/`。
"""
