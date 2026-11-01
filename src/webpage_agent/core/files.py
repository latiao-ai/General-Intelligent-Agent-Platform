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


# ---------- 工具结果的完整保存与受控读取（文档 observe-decide §4 第一步） ----------
# 为什么不能只靠摘要：观察要靠**真实清单**做判断。以前 `tool_results_text()` 按
# 字符截断，几百字之后的内容完全看不到，模型就会把"清单前三条"当成全部。
# 这里把完整结果落到本 run 的产物目录下，摘要里只留 `data_ref`；
# 需要细节时按引用读取，**不接受任意本地路径**。

RESULTS_DIRNAME = 'results'


def results_dir(folder, create: bool = True):
    """结果目录：跟着产物目录走，便于随任务一起清理与授权。"""
    from pathlib import Path
    path = Path(folder) / RESULTS_DIRNAME
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def save_result(folder, name: str, payload) -> str:
    """保存完整原始结果，返回**相对引用**（`results/<name>.json`）。

    相对引用而不是绝对路径：任务目录会被整体迁移/授权，绝对路径一旦搬到别处
    就失效，而且绝对路径等于把本机目录结构泄露给模型。
    """
    import re
    safe = re.sub(r'[^A-Za-z0-9_.-]+', '-', str(name or 'result'))[:80]
    path = results_dir(folder) / f'{safe}.json'
    save_json(path, {'payload': payload})
    return f'{RESULTS_DIRNAME}/{safe}.json'


def read_result(folder, ref: str, offset: int = 0, limit: int = 4000) -> dict:
    """按引用**有界**读取结果（默认 4000 字符），返回 `{text, total, next_offset}`。

    只允许读 `results/` 下的相对引用：越界（`..`、绝对路径、符号链接跳出去）
    一律拒绝——"模型要不要看更多"不能变成"模型能不能读本机任意文件"。
    """
    from pathlib import Path
    text_ref = str(ref or '').strip()
    if not text_ref or text_ref.startswith('/') or '..' in text_ref.split('/'):
        raise ValueError('结果引用必须是 results/ 下的相对路径')
    root = results_dir(folder, create=False).resolve()
    path = (Path(folder) / text_ref).resolve()
    if root not in path.parents and path != root:
        raise ValueError('结果引用越出结果目录')
    if not path.exists():
        raise FileNotFoundError(text_ref)
    total = json.dumps(__import__('json').loads(path.read_text(encoding='utf-8')),
                       ensure_ascii=False)
    start = max(0, int(offset or 0))
    chunk = total[start:start + max(200, int(limit or 4000))]
    return {'text': chunk, 'total': len(total),
            'next_offset': start + len(chunk) if start + len(chunk) < len(total) else None,
            'ref': text_ref}
