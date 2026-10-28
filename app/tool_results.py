"""通用工具结果：**信封 v2 + 受控存储 + 分块读取**。

为什么要有这一层（《智能体中台说明书》）：

- 旧实现要求 MCP 回执**必须是 JSON**（`result_payload()` 找不到 JSON 就抛错）。
  真实服务端返回一段合法文本、一张图片、一个资源链接是常事——因为"不是 JSON"
  就判失败，等于把合法返回当成故障；
- 旧实现只留一个**摘要**。观察节点要靠真实清单做判断，摘要被截断后模型
  就会把"前三条"当成全部；
- 旧实现没有"归属"。结果落在一个共享目录里，谁拿到引用谁都能读。

因此这一层做三件事，**都不涉及业务语义**：

1. **采集**（`stage_result`）：把 SDK 返回的 structuredContent、全部 content
   blocks、isError 原样保留下来，**不做逐字节抓包的承诺**；
2. **登记**（`commit_result_in_tx`）：在结算事务里写 manifest 与 parts，
   引用指向受控存储里的对象；
3. **读取**（`read_result`）：有界、可校验地读回。selector 只允许 part_id、
   JSON Pointer、offset/limit、期望 hash——**不接受文件路径、任意 URL 或表达式**。

三条纪律：

- **原始事实与模型投影分开**。摘要被截断不影响原结果；模型看到的是投影，
  需要细节时按引用受控读取；
- **缺合同不默认完成**。没有业务适配器判断时，业务状态是 `unknown`，
  不是 `succeeded`；
- **归属由登记上的企业和用户决定**，不由内容 hash 或目录名决定。相同内容不许因此共享读取权。
"""
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------- 状态词合同（§6 R1） ----------
# 传输层：协议上这次调用本身成没成。
TRANSPORT_STATUSES = ('ok', 'error', 'unknown')
# 业务层：远端业务推进到哪一步。**受理不等于完成**。
BUSINESS_STATUSES = ('not_applicable', 'accepted', 'running', 'succeeded',
                      'failed', 'cancelled', 'unknown')
# 校验层：平台有没有独立证据确认它真的成了。
VERIFICATION_STATUSES = ('not_applicable', 'unverified', 'verified', 'failed')

SCHEMA_VERSION = 2

# 结果存储的根目录名。放在产物目录下，随产物目录一起被授权与清理。
RESULTS_ROOT = '_tool_results'

# 单条结果的正文上限（字节）。超出时**明确标为不完整**并写明原因，
# 不能无限扩容，也不能悄悄截断——那样模型会以为拿到的是全部。
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
# 单次读取的分页上限。
DEFAULT_PAGE_LIMIT = 8192

_SAFE = re.compile(r'[^A-Za-z0-9_.-]+')


class ResultError(RuntimeError):
    """结果层的失败（引用无效、越权、体积超限等）。"""


@dataclass
class ResultPart:
    """结果的一个分块。

    `kind` 是内容的**类别**（json/text/image/audio/resource），不是 MIME。
    资源链接（`resource_link`）只保存引用：不主动去读——读它要走归属与网络规则。
    """

    ordinal: int
    kind: str
    content_type: str = ''
    mime: str = ''
    text: str = ''
    data: bytes | None = None
    link_ref: str = ''
    metadata: dict = field(default_factory=dict)
    visibility: str = 'owner'
    size_bytes: int = 0
    sha256: str = ''

    def is_binary(self) -> bool:
        return self.kind in {'image', 'audio', 'binary'}


@dataclass
class CapturedResult:
    """从 SDK 返回里**原样**采集到的东西。

    没有业务判断：业务状态由适配器判定（见 `app/external_tasks.py` 与
    `app/capabilities/publishing.py`），这里只负责"别把东西弄丢"。
    """

    structured: dict | None = None
    parts: list[ResultPart] = field(default_factory=list)
    is_error: bool = False
    unsupported: list[str] = field(default_factory=list)

    def text(self) -> str:
        """全部文本分块拼起来（没有文本块时返回空串）。"""
        return '\n'.join(part.text for part in self.parts
                         if part.kind == 'text' and part.text)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------- 采集（R2） ----------

def capture_mcp_result(result) -> CapturedResult:
    """把 SDK 的 `CallToolResult` 采集成 `CapturedResult`。

    保留：structuredContent、**全部** content blocks（含多块文本、图片、音频、
    资源链接、内嵌资源）、isError。

    不保留的：SDK 对象本身的内部细节（我们只保存可序列化的结构）。
    未知 block 类型保留其可序列化结构并标 `unsupported`——模型不自动用它，
    但它**不会因为不认识就被丢掉**。
    """
    captured = CapturedResult()
    captured.is_error = bool(getattr(result, 'isError', False))
    structured = getattr(result, 'structuredContent', None)
    if isinstance(structured, dict):
        captured.structured = structured
    for index, block in enumerate(getattr(result, 'content', None) or []):
        part = _capture_block(block, index)
        if part is None:
            continue
        captured.parts.append(part)
    return captured


def _capture_block(block, index: int) -> ResultPart | None:
    """一个 content block → 一个分块。"""
    kind = str(getattr(block, 'type', '') or 'text')
    if kind == 'text':
        text = str(getattr(block, 'text', '') or '')
        return ResultPart(ordinal=index, kind='text',
                          content_type='text/plain', text=text,
                          size_bytes=len(text.encode('utf-8')))
    if kind in ('image', 'audio'):
        mime = str(getattr(block, 'mimeType', '') or
                   f'{kind}/{"png" if kind == "image" else "wav"}')
        data = getattr(block, 'data', '') or ''
        try:
            raw = base64_decode(data)
        except Exception:                            # noqa: BLE001
            raw = b''
        return ResultPart(ordinal=index, kind=kind, content_type=mime, mime=mime,
                          data=raw, size_bytes=len(raw),
                          sha256=_sha256(raw) if raw else '')
    if kind == 'resource':
        resource = getattr(block, 'resource', None)
        text = str(getattr(resource, 'text', '') or '')
        mime = str(getattr(resource, 'mimeType', '') or '')
        uri = str(getattr(resource, 'uri', '') or '')
        if text:
            return ResultPart(ordinal=index, kind='text', content_type=mime or
                              'text/plain', mime=mime, text=text,
                              size_bytes=len(text.encode('utf-8')),
                              metadata={'resource_uri': uri} if uri else {})
        blob = getattr(resource, 'blob', '') or ''
        try:
            raw = base64_decode(blob)
        except Exception:                            # noqa: BLE001
            raw = b''
        return ResultPart(ordinal=index, kind='resource',
                          content_type=mime or 'application/octet-stream',
                          mime=mime, data=raw, size_bytes=len(raw),
                          sha256=_sha256(raw) if raw else '',
                          metadata={'resource_uri': uri} if uri else {})
    if kind == 'resource_link':
        uri = str(getattr(block, 'uri', '') or '')
        mime = str(getattr(block, 'mimeType', '') or '')
        return ResultPart(ordinal=index, kind='resource_link',
                          content_type=mime, mime=mime, link_ref=uri,
                          metadata={'name': str(getattr(block, 'name', '') or '')})
    # 未知 block：保留能序列化的结构，标 unsupported。
    return ResultPart(ordinal=index, kind='unsupported',
                      content_type='application/json',
                      text=json.dumps(_safe_repr(block), ensure_ascii=False),
                      metadata={'block_type': kind})


def _safe_repr(block) -> dict:
    """把未知 block 变成可 JSON 化的结构（尽力而为，不抛错）。"""
    out: dict = {}
    for attr in ('type', 'name', 'uri', 'mimeType', 'text'):
        value = getattr(block, attr, None)
        if isinstance(value, (str, int, float, bool)):
            out[attr] = value
    model_dump = getattr(block, 'model_dump', None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode='json')
            if isinstance(dumped, dict):
                for key, value in dumped.items():
                    if isinstance(value, (str, int, float, bool)) or value is None:
                        out.setdefault(key, value)
        except Exception:                            # noqa: BLE001
            pass
    return out


def base64_decode(data: str) -> bytes:
    import base64
    return base64.b64decode(data or '', validate=False)


# ---------- 受控存储 ----------

def results_root(base_dir) -> Path:
    """结果存储根目录。按租户分目录：跨租户连**路径**都不共享。"""
    return Path(base_dir) / RESULTS_ROOT


def tenant_segment(tenant_id: str) -> str:
    """租户目录名。哈希的是原始租户号，替换字符或截断之后再取名会把不同租户叠到一起。"""
    return hashlib.sha256(str(tenant_id or 'default').encode('utf-8')).hexdigest()


def result_segment(result_id: str) -> str:
    text = str(result_id or '')
    if not text:
        raise ResultError('result_id 非法')
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _legacy_tenant_segment(tenant_id: str) -> str:
    """旧目录名：非安全字符换成 `-`，再截到 40。只用于认已有登记，新文件不再这么写。"""
    return _SAFE.sub('-', str(tenant_id or 'default'))[:40] or 'default'


def _legacy_result_segment(result_id: str) -> str:
    return _SAFE.sub('-', str(result_id or ''))[:64]


def _plain_segment(value: str) -> str:
    """旧 storage_key 里的结果编号。含路径或替换字符的值不能当目录段。"""
    text = str(value or '')
    if not text or text in ('.', '..') or _SAFE.search(text):
        return ''
    return text


def result_locations(tenant_id: str, result_id: str) -> set[tuple[str, str]]:
    """这个结果可以出现的目录段。

    新文件只用哈希。已经登记的旧 key 仍指向替换、截断后的目录，读取时认那一版，
    不把字节搬进新目录。没登记的临时目录留给清理，不迁进可读位置。
    """
    pairs = {(tenant_segment(tenant_id), result_segment(result_id))}
    legacy_tenant = _legacy_tenant_segment(tenant_id)
    legacy_result = _legacy_result_segment(result_id)
    if legacy_result and legacy_result not in ('.', '..'):
        pairs.add((legacy_tenant, legacy_result))
    raw = _plain_segment(result_id)
    if raw:
        pairs.add((legacy_tenant, raw))
    return pairs


def result_dir(base_dir, tenant_id: str, result_id: str) -> Path:
    return (results_root(base_dir) / tenant_segment(tenant_id)
            / result_segment(result_id))


def relative_result_file(tenant_id: str, result_id: str, filename: str) -> str:
    """登记进库的相对路径。文件名只能是这一层，不能带目录。"""
    name = str(filename or '')
    if not name or name in ('.', '..') or Path(name).name != name:
        raise ResultError('结果文件名非法')
    return (f'{RESULTS_ROOT}/{tenant_segment(tenant_id)}'
            f'/{result_segment(result_id)}/{name}')


def open_registered_file(base_dir, *, tenant_id: str, result_id: str,
                         storage_key: str) -> Path:
    """打开登记上的相对路径，并确认文件就在这个结果自己的目录里。

    调用方传来的 result_id 只用来查登记。真正打开哪个文件，以登记里的 key 为准，
    而且这个 key 必须落在该结果允许的目录段中。指到别的结果、别的租户，或带
    `..` 的，都拒绝。
    """
    key = str(storage_key or '').strip().replace('\\', '/')
    parts = key.split('/')
    if (not key or len(parts) != 4
            or any(part in ('', '.', '..') for part in parts)
            or parts[0] != RESULTS_ROOT):
        raise ResultError('结果引用无效')
    if (parts[1], parts[2]) not in result_locations(tenant_id, result_id):
        raise ResultError('结果引用越出结果目录')
    root = (results_root(base_dir) / parts[1] / parts[2]).resolve()
    path = (Path(base_dir) / key).resolve()
    if root not in path.parents:
        raise ResultError('结果引用越出结果目录')
    return path


def new_result_id() -> str:
    return 'res_' + uuid.uuid4().hex[:16]


def stage_result(base_dir, captured: CapturedResult, *, tenant_id: str = 'default',
                 result_id: str = '', max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """把采集到的结果**落到受控存储**，返回待登记的 manifest 与 parts。

    返回值是纯数据，可以直接交给 `commit_result_in_tx()` 在结算事务里写库。

    体积超限时：**不截断**。超限的分块不落盘，manifest 标记
    `complete=false` 并写明原因——模型会看到"这份结果不完整"，而不是
    拿到一份看起来完整、其实缺了尾巴的数据。
    """
    result_id = result_id or new_result_id()
    folder = result_dir(base_dir, tenant_id, result_id)
    folder.mkdir(parents=True, exist_ok=True)

    parts: list[dict] = []
    total = 0
    truncated_reason = ''
    ordinal = 0
    for part in captured.parts:
        if part.ordinal != ordinal:
            ordinal = part.ordinal
        payload = part.data if part.data is not None else part.text.encode('utf-8')
        size = len(payload)
        if total + size > max_bytes:
            truncated_reason = (f'结果超过单条上限 {max_bytes} 字节，'
                                f'第 {part.ordinal} 个分块未保存')
            break
        total += size
        suffix = _suffix_for(part)
        filename = f'part-{part.ordinal:03d}{suffix}'
        (folder / filename).write_bytes(payload)
        parts.append({
            'part_id': f'{result_id}-p{part.ordinal}',
            'ordinal': part.ordinal,
            'content_type': part.content_type,
            'kind': part.kind,
            'mime': part.mime,
            'storage_key': relative_result_file(tenant_id, result_id, filename),
            'link_ref': part.link_ref,
            'size_bytes': size,
            'sha256': _sha256(payload),
            'metadata_json': json.dumps(part.metadata, ensure_ascii=False),
            'visibility': part.visibility,
        })

    raw_ref = ''
    if captured.structured is not None:
        raw = json.dumps(captured.structured, ensure_ascii=False,
                         default=str).encode('utf-8')
        if len(raw) <= max_bytes:
            (folder / 'structured.json').write_bytes(raw)
            raw_ref = relative_result_file(tenant_id, result_id, 'structured.json')

    completeness = {
        'complete': not truncated_reason,
        'stored_bytes': total,
        'max_bytes': max_bytes,
        'reason': truncated_reason,
        'part_count': len(parts),
        'declared_part_count': len(captured.parts),
        'unsupported_blocks': captured.unsupported,
    }
    (folder / 'manifest.json').write_text(json.dumps({
        'result_id': result_id, 'parts': parts, 'completeness': completeness,
        'registered': True, 'created_at': time.time(),
    }, ensure_ascii=False), encoding='utf-8')
    return {'result_id': result_id, 'parts': parts, 'raw_ref': raw_ref,
            'completeness': completeness, 'total_bytes': total}


def _suffix_for(part: ResultPart) -> str:
    if part.kind == 'text':
        return '.txt'
    if part.kind == 'json':
        return '.json'
    if part.kind == 'image':
        return '.' + (part.mime.split('/')[-1] if '/' in part.mime else 'bin')
    if part.kind == 'audio':
        return '.' + (part.mime.split('/')[-1] if '/' in part.mime else 'bin')
    if part.kind == 'resource_link':
        return '.link'
    return '.bin'


# ---------- 登记（提交事务里做） ----------

async def commit_result_in_tx(conn, context: dict, staged: dict, envelope: dict) -> str:
    """在**结算事务**里登记 manifest 与 parts，返回 result_id。

    为什么必须在同一个事务：结果文件已经落盘，数据库却没登记，崩溃后就留下
    一份"没人引用"的临时对象（可延迟回收）；反过来登记了但文件没落盘，引用
    会指向空路径。两者都不该发生，所以登记与结算同事务。
    """
    from app.pg_store import now

    result_id = staged['result_id']
    await conn.execute(
        'INSERT INTO tool_result_manifests '
        '(result_id, tenant_id, owner_user_id, run_id, step_id, action_id, '
        ' attempt_id, tool_key, schema_hash, contract_version, transport_status, '
        ' business_status, verification_status, raw_ref, structured_ref, '
        ' summary_json, completeness_json, schema_version, size_bytes, created_at) '
        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
        'ON CONFLICT (result_id) DO NOTHING',
        (result_id,
         str(context.get('tenant_id') or 'default'),
         str(context.get('owner_user_id') or 'default'),
         str(context.get('run_id') or ''),
         str(context.get('step_id') or ''),
         str(context.get('action_id') or ''),
         str(context.get('attempt_id') or ''),
         str(envelope.get('tool_key') or ''),
         str(envelope.get('schema_hash') or ''),
         str(envelope.get('contract_version') or ''),
         str(envelope.get('transport_status') or 'ok'),
         str(envelope.get('business_status') or 'not_applicable'),
         str(envelope.get('verification_status') or 'not_applicable'),
         str(staged.get('raw_ref') or ''),
         str(envelope.get('structured_ref') or ''),
         json.dumps(envelope.get('summary') or {}, ensure_ascii=False, default=str),
         json.dumps(staged.get('completeness') or {}, ensure_ascii=False),
         SCHEMA_VERSION,
         int(staged.get('total_bytes') or 0),
         now()))
    for part in staged.get('parts') or []:
        await conn.execute(
            'INSERT INTO tool_result_parts '
            '(part_id, result_id, ordinal, content_type, kind, mime, storage_key, '
            ' link_ref, size_bytes, sha256, metadata_json, visibility) '
            'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
            'ON CONFLICT (part_id) DO NOTHING',
            (part['part_id'], result_id, part['ordinal'], part['content_type'],
             part['kind'], part['mime'], part['storage_key'], part['link_ref'],
             part['size_bytes'], part['sha256'], part['metadata_json'],
             part['visibility']))
    return result_id


# ---------- 信封 ----------

def envelope_v2(*, result_id: str, tool_key: str, action_id: str = '',
                attempt_id: str = '', transport_status: str = 'ok',
                business_status: str = 'not_applicable',
                verification_status: str = 'not_applicable',
                summary: dict | None = None, content_refs=None,
                structured_ref: str = '', data_ref: str = '',
                external_task_ref: str = '', artifact_refs=None,
                complete: bool = True, error: dict | None = None,
                completeness: dict | None = None) -> dict:
    """构造 schema_version=2 的结果信封。

    字段名与取值是**合同**：观察、收尾、API 与前端都按它读。旧字段
    （`execution_status` / `phase`）由 `legacy_projection()` 投影，不在这里
    重复定义——否则两套词汇迟早分叉。
    """
    return {
        'schema_version': SCHEMA_VERSION,
        'result_id': result_id,
        'action_id': action_id,
        'attempt_id': attempt_id,
        'tool_key': tool_key,
        'transport_status': transport_status,
        'business_status': business_status,
        'verification_status': verification_status,
        'summary': summary if summary is not None else {},
        'content_refs': list(content_refs or []),
        'structured_ref': structured_ref,
        'data_ref': data_ref,
        'external_task_ref': external_task_ref,
        'artifact_refs': list(artifact_refs or []),
        'complete': bool(complete),
        'completeness': completeness or {},
        'error': error or {},
    }


def legacy_projection(envelope: dict) -> dict:
    """把 v2 信封投影成旧字段（`execution_status` / `phase`）。

    投影规则**不能**让"受理"变成"成功"：

    | business_status | execution_status | phase |
    |---|---|---|
    | succeeded | succeeded | completed |
    | accepted / running | accepted | accepted / processing |
    | failed / cancelled | failed | '' |
    | unknown | unknown | '' |

    旧读取点（观察、完成核对、outcome）先看旧字段，但**旧字段不能反过来
    把 accepted 推成成功**——那正是旧实现最危险的兜底。
    """
    business = str(envelope.get('business_status') or 'not_applicable')
    transport = str(envelope.get('transport_status') or 'ok')
    if business == 'succeeded':
        status, phase = 'succeeded', 'completed'
    elif business in ('accepted', 'running'):
        status, phase = 'accepted', ('processing' if business == 'running'
                                     else 'accepted')
    elif business in ('failed', 'cancelled'):
        status, phase = 'failed', ''
    elif business == 'unknown' or transport == 'unknown':
        status, phase = 'unknown', ''
    elif transport == 'error':
        status, phase = 'failed', ''
    else:
        status, phase = 'succeeded', 'completed'
    return {'execution_status': status, 'phase': phase,
            'business_status': business,
            'transport_status': transport}


# ---------- 读取（受控） ----------

def read_structured(base_dir, manifest: dict):
    """读登记上的 structured/raw 引用。JSON 损坏时返回 None，路径不对则拒绝。"""
    ref = str((manifest or {}).get('raw_ref') or '') or str(
        (manifest or {}).get('structured_ref') or '')
    if not ref:
        raise ResultError('结果不存在')
    path = open_registered_file(
        base_dir, tenant_id=str(manifest.get('tenant_id') or 'default'),
        result_id=str(manifest.get('result_id') or ''), storage_key=ref)
    if not path.is_file():
        raise ResultError('结果不存在')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except ValueError:
        return None
    except OSError as exc:
        raise ResultError('结果不存在') from exc


def read_part(base_dir, tenant_id: str, part: dict, *, result_id: str,
              offset: int = 0, limit: int = DEFAULT_PAGE_LIMIT) -> dict:
    """读一个分块（有界）。

    `offset`/`limit` 是**字节**偏移；返回 `total` 与 `next_read_ref`，
    让调用方知道"还有多少、接着从哪读"。路径必须落在这个 result_id 自己的目录里。
    """
    path = open_registered_file(
        base_dir, tenant_id=tenant_id, result_id=result_id,
        storage_key=str(part.get('storage_key') or ''))
    if not path.is_file():
        raise ResultError('结果分块不存在')
    payload = path.read_bytes()
    start = max(0, int(offset or 0))
    size = max(1, min(int(limit or DEFAULT_PAGE_LIMIT), DEFAULT_PAGE_LIMIT))
    chunk = payload[start:start + size]
    try:
        text = chunk.decode('utf-8')
        binary = False
    except UnicodeDecodeError:
        text = ''
        binary = True
    end = start + len(chunk)
    return {
        'part_id': part.get('part_id'),
        'kind': part.get('kind'),
        'mime': part.get('mime') or part.get('content_type') or '',
        'text': text,
        'binary': binary,
        'bytes': len(chunk),
        'total': len(payload),
        'complete': end >= len(payload),
        'next_read_ref': (None if end >= len(payload)
                          else {'part_id': part.get('part_id'), 'offset': end}),
        'sha256': part.get('sha256') or '',
    }


def project_for_model(manifest: dict, parts: list[dict] | None = None) -> dict:
    """给模型看的投影：有界摘要 + 引用，**不带**原始正文。

    投影必须显式说明"总量 vs 展示量"：否则模型会把"前 3 条"当成全部
    （这是旧实现里最常见的一类误判）。
    """
    summary = manifest.get('summary')
    if isinstance(summary, str):
        try:
            summary = json.loads(summary)
        except (TypeError, ValueError):
            summary = {'text': summary}
    completeness = manifest.get('completeness')
    if isinstance(completeness, str):
        try:
            completeness = json.loads(completeness)
        except (TypeError, ValueError):
            completeness = {}
    parts = parts or []
    return {
        'result_id': manifest.get('result_id'),
        'tool_key': manifest.get('tool_key'),
        'transport_status': manifest.get('transport_status'),
        'business_status': manifest.get('business_status'),
        'verification_status': manifest.get('verification_status'),
        'summary': summary or {},
        'part_count': len(parts),
        'parts': [{'part_id': item.get('part_id'), 'kind': item.get('kind'),
                   'mime': item.get('mime') or item.get('content_type'),
                   'size_bytes': item.get('size_bytes'),
                   'sha256': item.get('sha256')} for item in parts],
        'completeness': completeness or {},
        'read_hint': '用 read_tool_result 按 part_id / JSON Pointer 有界读取细节',
    }


def pointer_get(payload, pointer: str):
    """受限 JSON Pointer 取值。

    只认 `/a/b/0` 这种形态；**不执行表达式**、不访问文件系统。
    """
    if pointer in ('', '/'):
        return payload
    if not pointer.startswith('/'):
        raise ResultError('JSON Pointer 必须以 / 开头')
    value = payload
    for raw in pointer.lstrip('/').split('/'):
        token = raw.replace('~1', '/').replace('~0', '~')
        if isinstance(value, dict):
            if token not in value:
                raise ResultError(f'JSON Pointer 指向的字段不存在：{pointer}')
            value = value[token]
        elif isinstance(value, list):
            try:
                value = value[int(token)]
            except (ValueError, IndexError) as exc:
                raise ResultError(f'JSON Pointer 指向的下标不存在：{pointer}') from exc
        else:
            raise ResultError(f'JSON Pointer 指向的不是对象：{pointer}')
    return value


# ---------- 延迟回收（未引用的临时对象） ----------

def gc_unreferenced(base_dir, referenced: set[str], *, max_age_seconds: int = 86400) -> int:
    """删除**没有被引用**且超过保留期的结果文件。

    只删没被任何 manifest 引用的目录：活动动作/任务引用的结果不可回收。
    崩溃留下的临时对象（文件在、库引用没有）会在这里被清掉。
    """
    root = results_root(base_dir)
    if not root.exists():
        return 0
    now = time.time()
    removed = 0
    for tenant_dir in root.iterdir():
        if not tenant_dir.is_dir():
            continue
        for result_folder in tenant_dir.iterdir():
            if not result_folder.is_dir():
                continue
            if result_folder.name in referenced:
                continue
            try:
                age = now - result_folder.stat().st_mtime
            except OSError:
                continue
            if age < max_age_seconds:
                continue
            for item in result_folder.iterdir():
                try:
                    item.unlink()
                except OSError:
                    pass
            try:
                result_folder.rmdir()
                removed += 1
            except OSError:
                pass
    return removed
