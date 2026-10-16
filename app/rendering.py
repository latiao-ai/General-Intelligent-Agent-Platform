import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape
from app.config import settings, PROJECT_ROOT
from app.schemas import Report
from app.services import clean_public_url


def save_json(path: Path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8'
    )


def report_errors(report: Report, sources: list[dict]):
    ids = {s['source_id'] for s in sources if s['status'] == 'success'}
    errors = []
    citations = [report.summary_source_ids]
    for section in report.sections:
        citations.extend(p.source_ids for p in section.paragraphs)
    citations.extend(row.source_ids for row in report.comparison_table.rows)
    for group in citations:
        if not group or any(i not in ids for i in group):
            errors.append('引用缺失或包含未知/失败来源')
    table = report.comparison_table
    if table.rows and not table.columns:
        errors.append('表格缺少列标题')
    if any(len(row.cells) != len(table.columns) for row in table.rows):
        errors.append('表格列数不匹配')
    return list(dict.fromkeys(errors))


def render_report(task_id: str, report: Report, documents: list[dict]):
    problems = report_errors(report, documents)
    if problems:
        raise ValueError('；'.join(problems))
    folder = settings.artifact_dir / task_id
    folder.mkdir(parents=True, exist_ok=True)
    sources = []
    for source in documents:
        if source['status'] == 'success':
            item = {k: v for k, v in source.items() if k != 'raw_content'}
            item['url'] = clean_public_url(item['url'])
            sources.append(item)
    env = Environment(
        loader=FileSystemLoader(PROJECT_ROOT / 'app' / 'templates'),
        autoescape=select_autoescape(['html', 'xml']),
    )
    html = env.get_template('report.html').render(
        report=report.model_dump(), sources=sources,
        generated_at=datetime.now(timezone.utc).isoformat(), task_id=task_id,
    )
    path = folder / 'index.html'
    path.write_text(html, encoding='utf-8')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    artifact = {
        'task_id': task_id, 'html_path': str(path.resolve()),
        'sha256': digest, 'size_bytes': path.stat().st_size,
    }
    save_json(folder / 'report.json', report.model_dump())
    save_json(folder / 'sources.json', documents)
    save_json(folder / 'manifest.json', artifact)
    return artifact
