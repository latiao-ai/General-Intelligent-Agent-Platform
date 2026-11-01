"""W3C request correlation and structured spans, without logging request bodies."""
import json
import logging
import re
import secrets
from contextvars import ContextVar
from datetime import datetime, timezone

trace_id = ContextVar('trace_id', default='')
span_id = ContextVar('span_id', default='')
run_id = ContextVar('trace_run_id', default='')
_TRACEPARENT = re.compile(r'^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$')


def new_trace(parent=''):
    match = _TRACEPARENT.fullmatch(parent)
    if match and int(match[1], 16) and int(match[2], 16):
        return match[1]
    return secrets.token_hex(16)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {'time': datetime.now(timezone.utc).isoformat(),
                   'level': record.levelname, 'logger': record.name,
                   'message': record.getMessage(), 'trace_id': trace_id.get(),
                   'span_id': span_id.get(), 'run_id': run_id.get()}
        if record.exc_info:
            # Avoid dumping third party exception payloads that may carry credentials.
            payload['exception_type'] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False)


def prometheus(stats):
    lines = []
    for name, value in sorted(stats.items()):
        if isinstance(value, dict):
            for key, count in sorted(value.items()):
                if isinstance(count, (int, float)):
                    label = json.dumps(str(key), ensure_ascii=False)
                    lines.append(f'webpage_{name}{{state={label}}} {count}')
        elif isinstance(value, (int, float)):
            lines.extend((f'# TYPE webpage_{name} gauge', f'webpage_{name} {value}'))
    return '\n'.join(lines) + '\n'
