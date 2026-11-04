# 第三方依赖与许可证清单

本文件列出运行期依赖（`requirements.lock.txt` 逐条固定版本）及其许可证，供交付、
安全评审与合规审查使用。

**怎么来的**：逐条读该版本安装后自带的元数据（`License-Expression` 字段、`License`
字段、`License ::` 分类器）与随包 `LICENSE` / `COPYING` 正文，取能互相印证的结论。
没有联网查、没有凭印象填、也没有从别的项目抄。个别包多个来源写法不同（例如
`python-dateutil` 的元数据只写 "Dual License"），按随包 LICENSE 正文与分类器合并
记录，并在下面「需要留意的地方」里点名。

**这不是法律意见**。对外发布、商业化或嵌入他人产品前，请让法务按实际分发方式
（SaaS / 私有部署 / 随包分发）复核一遍。

## 清单

| 包 | 版本 | 许可证 |
|---|---|---|
| `altair` | 6.3.0 | BSD-3-Clause |
| `annotated-doc` | 0.0.5 | MIT |
| `annotated-types` | 0.8.0 | MIT |
| `anyio` | 4.15.1 | MIT |
| `attrs` | 26.1.0 | MIT |
| `certifi` | 2026.7.22 | MPL-2.0 |
| `cffi` | 2.1.1 | MIT-0 |
| `charset-normalizer` | 3.5.2 | MIT |
| `click` | 8.5.0 | BSD-3-Clause |
| `cryptography` | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| `distro` | 1.9.0 | Apache-2.0 |
| `fastapi` | 0.143.2 | MIT |
| `h11` | 0.16.0 | MIT |
| `httpcore2` | 2.13.1 | BSD-3-Clause |
| `httpcore` | 1.0.9 | BSD-3-Clause |
| `httptools` | 0.9.0 | MIT |
| `httpx-sse` | 0.4.3 | MIT |
| `httpx2` | 2.13.1 | BSD-3-Clause |
| `httpx` | 0.28.1 | BSD-3-Clause |
| `idna` | 3.20 | BSD-3-Clause |
| `itsdangerous` | 2.2.0 | BSD-3-Clause |
| `Jinja2` | 3.1.6 | BSD-3-Clause |
| `jiter` | 0.17.0 | MIT |
| `jsonpatch` | 1.35 | BSD-3-Clause |
| `jsonpointer` | 3.2.1 | BSD-3-Clause |
| `jsonschema-specifications` | 2025.9.1 | MIT |
| `jsonschema` | 4.26.0 | MIT |
| `langchain-core` | 1.6.10 | MIT |
| `langchain-openai` | 1.7.1 | MIT |
| `langchain-protocol` | 0.0.19 | MIT |
| `langgraph-checkpoint` | 4.3.0 | MIT |
| `langgraph-prebuilt` | 1.1.0 | MIT |
| `langgraph-sdk` | 0.4.7 | MIT |
| `langgraph` | 1.2.15 | MIT |
| `langsmith` | 0.14.8 | MIT |
| `MarkupSafe` | 3.0.4 | BSD-3-Clause |
| `mcp` | 1.30.0 | MIT |
| `narwhals` | 2.27.1 | MIT |
| `numpy` | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| `openai` | 3.31.0 | Apache-2.0 |
| `opentelemetry-api` | 1.45.1 | Apache-2.0 |
| `orjson` | 3.13.0 | MPL-2.0 AND (Apache-2.0 OR MIT) |
| `ormsgpack` | 1.12.2 | Apache-2.0 OR MIT |
| `packaging` | 26.3 | Apache-2.0 OR BSD-2-Clause |
| `pandas` | 3.0.6 | BSD-3-Clause |
| `pillow` | 12.3.0 | MIT-CMU |
| `protobuf` | 7.36.2 | BSD-3-Clause |
| `psycopg-binary` | 3.3.6 | LGPL-3.0-only |
| `psycopg-pool` | 3.3.3 | LGPL-3.0-only |
| `psycopg` | 3.3.6 | LGPL-3.0-only |
| `pyarrow` | 25.0.1 | Apache-2.0 |
| `pycparser` | 3.11 | BSD-3-Clause |
| `pydantic-settings` | 2.15.0 | MIT |
| `pydantic` | 2.14.1 | MIT |
| `pydantic_core` | 2.50.1 | MIT |
| `pydeck` | 0.9.3 | Apache-2.0 |
| `PyJWT` | 2.15.1 | MIT |
| `python-dateutil` | 2.9.0.post0 | Apache-2.0 OR BSD-3-Clause（元数据写 Dual License，分类器为 BSD + Apache） |
| `python-dotenv` | 1.2.4 | BSD-3-Clause |
| `python-multipart` | 0.0.32 | Apache-2.0 |
| `PyYAML` | 6.0.3 | MIT |
| `referencing` | 0.37.0 | MIT |
| `regex` | 2026.9.29 | Apache-2.0 AND CNRI-Python |
| `requests-toolbelt` | 1.0.0 | Apache-2.0 |
| `requests` | 2.34.2 | Apache-2.0 |
| `rpds-py` | 2026.9.1 | MIT |
| `setuptools` | 84.0.0 | MIT |
| `six` | 1.17.0 | MIT |
| `sniffio` | 1.3.1 | MIT OR Apache-2.0 |
| `sse-starlette` | 3.5.0 | BSD-3-Clause |
| `starlette` | 1.8.0 | BSD-3-Clause |
| `tenacity` | 9.2.1 | Apache-2.0 |
| `tiktoken` | 0.14.0 | MIT |
| `toml` | 0.10.2 | MIT |
| `truststore` | 0.10.5 | MIT |
| `typing-inspection` | 0.4.4 | MIT |
| `typing_extensions` | 4.16.0 | PSF-2.0 |
| `urllib3` | 2.8.0 | MIT |
| `uuid_utils` | 0.17.1 | BSD-3-Clause |
| `uvicorn` | 0.54.0 | BSD-3-Clause |
| `websockets` | 16.1.1 | BSD-3-Clause |
| `wheel` | 0.47.0 | MIT |
| `xxhash` | 4.0.1 | BSD-2-Clause |
| `zstandard` | 0.25.0 | BSD-3-Clause |

## 需要留意的地方

### psycopg 系列是 LGPL-3.0（唯一不是宽松许可证的依赖）

`psycopg`、`psycopg-binary`、`psycopg-pool` 均为 **LGPL-3.0-only**。当前用法符合
LGPL 对"以库形式使用"的要求：

- **动态使用，不静态嵌入**：作为独立的第三方包安装，本项目代码 `import psycopg`
  调用其公开接口，不复制、不修改其源码，也没有把它的代码编译进本项目的产物。
- **可替换**：部署产物里它是独立目录（`site-packages/psycopg*`），替换版本不需要
  重新构建本项目；发布包里同样保留该目录结构。
- **不修改**：本仓库没有任何对 psycopg 源码的补丁。

因此本项目自身的代码不因链接 LGPL 库而需要以 LGPL 发布。若将来改成随可执行文件
静态打包（例如 PyInstaller 单文件嵌入 psycopg 二进制），或者修改了 psycopg 源码，
这一结论就要重新评估——那两点都会触发 LGPL 的额外义务。

`psycopg-binary` 是该库官方发布的预编译 wheel（同属 LGPL-3.0）。纯 Python 的
`psycopg` 也可以配 libpq 源码安装，但锁定文件走的是 binary 包。

### 其他需要留意的项

- **MPL-2.0 文件级 copyleft**：`certifi`、`orjson`（另含 Apache-2.0 或 MIT 部分）。
  MPL 的义务落在"被修改的源文件"上，本项目未修改它们，按原样分发即可。
- **`numpy` 是复合许可证**：`BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0`，
  随包还有 `COPYING` 说明捆绑的组件，分发时需要一并保留。
- **`regex` 是 `Apache-2.0 AND CNRI-Python`**：CNRI-Python 许可证有署名要求，随包
  LICENSE 已包含，保留即可。
- **`pillow` 使用 MIT-CMU**（Pillow 自己的 MIT 变体），不是标准 MIT 措辞。
- **`cffi` 是 MIT-0**（去掉署名要求的 MIT），比 MIT 更宽松。
- **`python-dateutil`** 元数据只写 "Dual License"，分类器为 BSD + Apache；按
  `Apache-2.0 OR BSD-3-Clause` 记录。
- **`cryptography`** 为 `Apache-2.0 OR BSD-3-Clause`，二选一即可。
- 若将来升级任一依赖，请重新生成本文件；许可证会随版本变（例如某个包从 BSD 换成
  Apache）。清单的版本必须与 `requirements.lock.txt` 完全一致才有意义。

## 本项目自己的许可证

仓库里**没有** `LICENSE` 文件，`pyproject.toml` 也没写 `license` 字段——也就是说
本项目的授权方式尚未确定。这不影响上面的第三方清单，但在对外交付前需要由所有者
拍板（见阶段 0 报告里的待确认项）。本文件不代替、也不预设那个决定。
