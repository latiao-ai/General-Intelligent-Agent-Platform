"""files 能力包：把模型自己产出的内容落成文件。

为什么需要它：很多"制作类"服务端只提供**执行**动作（合成、配音、字幕），
并不提供"写剧本/写分镜"这类创作接口。模型如果假设服务端有 `generate_script`，
只会拿到一堆 `E_RESOURCE_NOT_FOUND`。正确做法是让模型自己把内容写出来，用这个
工具落盘，再把真实存在的执行工具用起来。

写入范围被限制在 `artifacts/<task_id>/workspace/` 下：不接受绝对路径，也不允许
`..` 逃逸——工具能写文件，就必须同时把边界写死。
"""
import re
from pathlib import Path

from app.capabilities.base import ToolError
from app.capabilities.registry import tool
from app.config import settings
from app.events import action, emit

CAPABILITY = 'files'

PROMPT_RULES = (
    '【写文件】剧本、分镜、旁白文案、说明这类**创作内容由你自己写**，写完用 '
    'write_file 保存成文件（相对路径，会落在本任务的工作目录下）。'
    'MCP 服务端通常只提供"执行"动作（合成/配音/字幕/渲染），不要假设它有'
    '"生成脚本/生成分镜"的接口；需要文件时先自己写、再交给真实存在的工具。'
)

# 单文件上限：一次调用写这么多已经远超正常文案需求，再大就该分段
MAX_CHARS = 200_000
_SAFE_NAME = re.compile(r'^[\w.\-\u4e00-\u9fff/]+$')


@tool(name='write_file', label='写文件', effect='write', key='local:files:write',
      description='把一段文本（剧本/分镜/文案/配置等）保存成本任务工作目录下的文件。'
                  'path 用相对路径，例如 script.md、storyboard/scene-01.md。',
      args={'type': 'object',
            'properties': {
                'path': {'type': 'string',
                         'description': '相对路径，只允许字母数字、中文、. - _ /'},
                'content': {'type': 'string', 'description': '文件内容'},
            },
            'required': ['path', 'content']})
async def _write_file(args: dict, state: dict) -> dict:
    path = str(args.get('path') or '').strip()
    content = args.get('content')
    if not path:
        raise ToolError('write_file 需要 path（相对路径）')
    if not isinstance(content, str) or not content.strip():
        raise ToolError('write_file 需要 content（非空文本）')
    if len(content) > MAX_CHARS:
        raise ToolError(f'内容太长（{len(content)} 字符），请拆成多个文件')
    if path.startswith(('/', '~')) or '..' in Path(path).parts:
        raise ToolError('path 必须是工作目录内的相对路径，不接受绝对路径或 ..')
    if not _SAFE_NAME.match(path):
        raise ToolError('path 只允许字母、数字、中文与 . - _ /')

    folder = settings.artifact_dir / (state.get('task_id') or 'task') / 'workspace'
    target = (folder / path).resolve()
    # resolve 之后再确认一次：符号链接、编码花样都不能把写入带出工作目录
    if folder.resolve() not in target.parents and target != folder.resolve():
        raise ToolError('path 越出工作目录，已拒绝')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding='utf-8')

    size = target.stat().st_size
    emit(action('已写入文件', f'{path} · {size} 字节', '📄'))
    return {'written': {'path': str(target), 'relative': path, 'size_bytes': size},
            'status': 'written'}
