"""Periodic cleanup. Retain paused/uncertain work and the last event sequence."""
import asyncio
import json
import logging
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from webpage_agent.store import artifact_store
from webpage_agent.tools import tool_results
from webpage_agent.core.config import settings

logger = logging.getLogger(__name__)
FINAL = ('succeeded', 'failed', 'cancelled')


async def run_once(store, *, root=None, now_ts=None):
    root = Path(root or settings.artifact_dir).resolve()
    current = time.time() if now_ts is None else now_ts
    stamp = lambda days: datetime.fromtimestamp(current-days*86400, timezone.utc).isoformat()
    stats = {'events': 0, 'artifacts': 0, 'results': 0, 'orphans': 0}
    async with store._lease() as conn:
        row = await store._one(conn, 'SELECT pg_try_advisory_xact_lock(%s) AS locked', (719284632,))
        if not row['locked']:
            return stats
        await conn.execute('DELETE FROM api_rate_windows WHERE window_start < %s', (current-120,))
        if settings.retention_events_days:
            cur = await conn.execute(
                'DELETE FROM events e WHERE e.created_at < %s '
                'AND e.seq < (SELECT MAX(x.seq) FROM events x WHERE x.task_id=e.task_id) '
                'AND EXISTS (SELECT 1 FROM runs r WHERE r.task_id=e.task_id) '
                'AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.task_id=e.task_id '
                'AND r.status NOT IN (%s,%s,%s))', (stamp(settings.retention_events_days), *FINAL))
            stats['events'] = cur.rowcount
        if settings.retention_artifacts_days:
            cutoff = stamp(settings.retention_artifacts_days)
            rows = await store._all(conn,
                'SELECT t.task_id,t.artifact_id FROM tasks t WHERE t.updated_at < %s '
                'AND EXISTS (SELECT 1 FROM runs r WHERE r.task_id=t.task_id) '
                'AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.task_id=t.task_id '
                'AND (r.status NOT IN (%s,%s,%s) OR r.updated_at >= %s)) '
                'ORDER BY t.updated_at LIMIT 100 FOR UPDATE OF t SKIP LOCKED', (cutoff, *FINAL, cutoff))
            for task in rows:
                # Recheck under the same task lock used by create_run.
                await conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('retention:'+task['task_id'],))
                runs = await store._all(conn, 'SELECT * FROM runs WHERE task_id=%s FOR UPDATE', (task['task_id'],))
                if any(r['status'] not in FINAL or r['updated_at'] >= cutoff for r in runs):
                    continue
                for run in runs:
                    manifests = await store._all(conn, 'SELECT * FROM tool_result_manifests WHERE run_id=%s', (run['run_id'],))
                    for manifest in manifests:
                        for tenant, result in tool_results.result_locations(manifest['tenant_id'], manifest['result_id']):
                            relative = f'_tool_results/{tenant}/{result}'
                            try:
                                folder = artifact_store.contained(root, relative)
                            except artifact_store.PathDenied:
                                continue
                            if folder.is_dir():
                                try:
                                    marker = json.loads((folder / 'manifest.json').read_text())
                                except (OSError, ValueError):
                                    continue
                                if marker.get('result_id') != manifest['result_id']:
                                    continue
                                await asyncio.to_thread(shutil.rmtree, folder)
                        await conn.execute('DELETE FROM tool_result_parts WHERE result_id=%s', (manifest['result_id'],))
                        await conn.execute('DELETE FROM tool_result_manifests WHERE result_id=%s', (manifest['result_id'],))
                        stats['results'] += 1
                name = task.get('artifact_id') or ''
                if re.fullmatch(r'[a-f0-9]{32}', name):
                    folder = artifact_store.contained(root, name)
                    if folder.is_dir():
                        await asyncio.to_thread(shutil.rmtree, folder)
                        stats['artifacts'] += 1
                await conn.execute('DELETE FROM artifacts WHERE run_id IN '
                    '(SELECT run_id FROM runs WHERE task_id=%s)', (task['task_id'],))
        registered = await store._all(conn, 'SELECT result_id FROM tool_result_manifests')
        stats['orphans'] = await asyncio.to_thread(artifact_store.gc_unregistered, root,
            grace_seconds=settings.result_gc_grace_seconds, now_ts=current,
            registered_ids={r['result_id'] for r in registered})
    return stats


async def loop(store):
    while True:
        await asyncio.sleep(settings.maintenance_interval_seconds)
        try:
            result = await run_once(store)
            await store.purge_request_keys()
            await store.purge_auth_sessions()
            logger.info('maintenance %s', result)
        except Exception:
            logger.exception('maintenance_failed')
