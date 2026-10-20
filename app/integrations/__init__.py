"""外部系统对接层：MCP 服务端与 DeepSeek 官方检索接口。

与业务能力包（app/capabilities/）的区别：
- 这里只负责"连出去、拿到结构化结果"，不包含任何业务判断；
- 能力包在其之上组装语义（检索、抓正文、转网页、发布）。

依赖方向是单向的：capabilities → integrations，反向不允许。
"""
