"""Pi 工具参数 schema：收成供应商要求的 `type: object` 根。

模型端点要求 `tools[].function.parameters` 的根是 JSON Schema object。
根是 `anyOf` / `oneOf`（例如某些 MCP 工具把互斥必填写成裸联合）时，
整次请求 400，一个工具都调不了。目录装配时：

- 已经是 object（含 `type: object` 再带 oneOf/anyOf）原样保留，Node 编译器
  会把互斥必填折进同一个 object；
- 根是若干 object 变体的联合时，合并成一个 object（属性取并集，required
  取交集），互斥必填交给服务端在调用时校验；
- 收不成 object 的工具丢掉并告警，不让它拖垮整份目录。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def provider_parameters(schema: Any) -> tuple[dict | None, str]:
    """返回 `(可用 schema, 说明)`。说明为空表示原样可用；schema 为 None 表示丢掉。"""
    if not isinstance(schema, dict) or isinstance(schema, list):
        return None, '参数 schema 不是对象'
    if schema.get('type') == 'object':
        return schema, ''
    implicit = (schema.get('type') in (None, '')
                and isinstance(schema.get('properties'), dict)
                and 'anyOf' not in schema and 'oneOf' not in schema
                and 'allOf' not in schema)
    if implicit:
        normalized = dict(schema)
        normalized['type'] = 'object'
        return normalized, '补上缺失的 type:object'
    for key in ('anyOf', 'oneOf'):
        variants = schema.get(key)
        if isinstance(variants, list) and variants:
            merged = _merge_object_variants(variants)
            if merged is None:
                return None, f'参数根是 {key}，无法收成 type:object'
            return merged, f'参数根是 {key}，已合并为 type:object'
    if schema.get('type') in (None, '') and not any(
            schema.get(k) is not None for k in ('properties', 'anyOf', 'oneOf', 'allOf')):
        return {'type': 'object', 'properties': {}, 'additionalProperties': False}, \
            '空 schema 补成 object'
    return None, f'参数根不是 type:object（type={schema.get("type")!r}）'


def prepare_declarations(decls: list[dict] | None) -> tuple[list[dict], list[dict]]:
    """过滤/规范化一份工具声明。返回 `(保留, 丢弃)`，丢弃项含 name 与 reason。"""
    kept: list[dict] = []
    dropped: list[dict] = []
    for decl in decls or []:
        if not isinstance(decl, dict):
            dropped.append({'name': '', 'reason': '工具声明不是对象'})
            logger.warning('Pi 工具目录跳过一条声明：不是对象')
            continue
        name = str(decl.get('name') or '')
        params, note = provider_parameters(decl.get('parameters'))
        if params is None:
            logger.warning('Pi 工具目录跳过 %s：%s', name or '?', note)
            dropped.append({'name': name, 'reason': note})
            continue
        item = dict(decl)
        item['parameters'] = params
        if note:
            logger.warning('Pi 工具 %s 的参数 schema 已规范化：%s', name, note)
        kept.append(item)
    return kept, dropped


def _merge_object_variants(variants: list) -> dict | None:
    """把一组 object 变体收成一个 object。出现非 object 分支则放弃。"""
    objects: list[dict] = []
    for variant in variants:
        if _is_null_schema(variant):
            continue
        if not _is_object_schema(variant):
            return None
        objects.append(variant)
    if not objects:
        return None
    properties: dict[str, Any] = {}
    required_sets: list[list[str]] = []
    additional = False
    for node in objects:
        props = node.get('properties') if isinstance(node.get('properties'), dict) else {}
        for key, value in props.items():
            existing = properties.get(key)
            if existing is None or _is_null_schema(existing):
                properties[key] = value
        required = node.get('required')
        if isinstance(required, list) and required:
            required_sets.append([str(item) for item in required])
        if node.get('additionalProperties') is True:
            additional = True
    shared: list[str] = []
    if required_sets:
        shared = [key for key in required_sets[0]
                  if all(key in group for group in required_sets)]
    return {
        'type': 'object',
        'properties': properties,
        'required': shared,
        'additionalProperties': additional,
    }


def _is_null_schema(node: Any) -> bool:
    return isinstance(node, dict) and node.get('type') == 'null'


def _is_object_schema(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    if node.get('type') == 'object':
        return True
    return node.get('type') in (None, '') and isinstance(node.get('properties'), dict)
