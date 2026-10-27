"""MCP 接入约定：声明驱动，不按工具名或企业名猜测行为。"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from jsonschema import Draft202012Validator

META_KEY = 'agent_hub'
_action_id: ContextVar[str] = ContextVar('mcp_action_id', default='')


class ContractError(RuntimeError):
    certainty = 'not_executed'
    phase = 'validate'
    error_code = 'E_MCP_CONTRACT'

    @property
    def safe_message(self):
        return str(self)


@contextmanager
def action_scope(action_id: str):
    """只由账本准入后的执行器绑定；不能从模型参数里取动作身份。"""
    token = _action_id.set(action_id or '')
    try:
        yield
    finally:
        _action_id.reset(token)


def declaration(meta) -> dict:
    if not isinstance(meta, dict) or META_KEY not in meta:
        return {}
    value = meta[META_KEY]
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
        raise ContractError('agent_hub 接入声明必须是 version=1 的对象')
    unknown = set(value) - {'version', 'idempotency_field'}
    if unknown:
        raise ContractError('agent_hub 包含未支持的声明字段：' + ', '.join(sorted(unknown)))
    field = value.get('idempotency_field', '')
    if not isinstance(field, str) or (field and (not field.isidentifier() or field.startswith('_'))):
        raise ContractError('idempotency_field 必须是顶层参数名')
    return dict(value)


def model_schema(schema: dict, meta=None) -> dict:
    """模型只填业务参数，动作标识由执行器注入。原始 schema 保持不变。"""
    contract = declaration(meta)
    if not isinstance(schema, dict):
        raise ContractError('inputSchema 必须是 JSON Schema 对象')
    result = deepcopy(schema)
    field = contract.get('idempotency_field')
    if field:
        properties = result.get('properties') or {}
        if not isinstance(properties, dict) or not isinstance(result.get('required', []), list):
            raise ContractError('幂等字段要求有效的顶层 properties 和 required')
        prop = properties.get(field)
        if not isinstance(prop, dict) or prop.get('type') != 'string':
            raise ContractError('幂等字段必须在 inputSchema 中声明为 string')
        result['properties'].pop(field)
        if 'required' in result:
            result['required'] = [name for name in result['required'] if name != field]
    return result


def call_arguments(schema: dict, meta, arguments: dict) -> dict:
    contract = declaration(meta)
    model_schema(schema, meta)  # 同时校验声明；坏合同在发送前失败。
    result = dict(arguments or {})
    field = contract.get('idempotency_field')
    if field:
        action_id = _action_id.get()
        if not action_id:
            raise ContractError('工具要求可信动作标识，请通过中台任务执行，不能直接调用')
        if field in result:
            raise ContractError('动作标识由中台生成，模型和业务参数不得覆盖')
        result[field] = action_id
    return result


def inspect_tool(item: dict) -> list[dict]:
    """只检查工具描述，不调用业务工具。供管理员接入验收使用。"""
    issues = []
    def issue(level, code, message):
        issues.append({'level': level, 'code': code, 'message': message})
    if not str(item.get('description') or '').strip():
        issue('error', 'missing_description', '工具必须说明用途、限制与完成语义')
    for field in ('inputSchema', 'outputSchema'):
        schema = item.get(field)
        if not isinstance(schema, dict) or schema.get('type') != 'object':
            issue('error', 'invalid_' + field, field + ' 必须是 object JSON Schema')
            continue
        try:
            Draft202012Validator.check_schema(schema)
        except Exception:
            issue('error', 'invalid_' + field, field + ' 不是有效 JSON Schema')
    annotations = item.get('annotations') or {}
    if type(annotations.get('readOnlyHint')) is not bool:
        issue('error', 'missing_effect', '必须明确声明 readOnlyHint；不能按名字猜读写')
    elif annotations['readOnlyHint'] is False:
        for field in ('destructiveHint', 'idempotentHint'):
            if type(annotations.get(field)) is not bool:
                issue('error', 'missing_' + field, '写工具必须声明 ' + field)
    try:
        model_schema(item.get('inputSchema') or {}, item.get('_meta'))
    except ContractError as exc:
        issue('error', 'invalid_contract', str(exc))
    return issues
