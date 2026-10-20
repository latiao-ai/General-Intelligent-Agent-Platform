"""文件读写小工具。

原来放在 app/rendering.py 里，但那个模块的主体是"报告模板渲染"，
已随能力裁剪删除；`save_json` 留下来单独放在这里，名字也名副其实。
"""
import json
from pathlib import Path


def save_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8'
    )
