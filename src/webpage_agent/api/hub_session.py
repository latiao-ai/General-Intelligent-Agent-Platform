"""会话读侧 mixin：历史、会话列表、时间线、上一轮材料和停止指令。

`_artifact_view` 只有时间线在用，所以跟读侧放在一起，不跟恢复计划走。
方法体与拆分前逐字相同。
"""
import uuid
from pathlib import Path

from webpage_agent.api.task_record import TaskRecord
from webpage_agent.core import outcome as outcome_module

def _artifact_view(state: dict) -> dict | None:
    """把 state 里的产物压成前端够用的形状（不存在就 None）。"""
    artifact = (state or {}).get('artifact') or {}
    path = artifact.get('path') or artifact.get('html_path')
    if not path:
        return None
    return {'kind': artifact.get('kind') or 'report',
            'filename': Path(path).name,
            'size_bytes': artifact.get('size_bytes'),
            'exists': Path(path).exists()}


class HubSessionMixin:
    """Hub 的会话读侧。"""

    async def _assert_session_owner(self, session_id: str, tenant_id: str,
                                    user_id: str) -> None:
        """会话归属校验：不存在或不属于当前主体 → 一律 404（不泄漏存在性）。"""
        row = await self.store.session(session_id)
        if row is None:
            return                      # 全新会话 ID：允许（稍后创建时写入主人）
        owner = row.get('owner_user_id') or 'default'
        tenant = row.get('tenant_id') or 'default'
        if tenant != tenant_id or owner != user_id:
            raise PermissionError(f'会话 {session_id} 不属于当前主体')

    async def history(self, session_id: str) -> list:
        return await self.store.messages(session_id)

    async def list_sessions(self, limit: int = 50, principal=None) -> list[dict]:
        if principal is None:
            return await self.store.list_sessions(limit)
        return await self.store.list_sessions(
            limit, tenant_id=principal.tenant_id, owner_user_id=principal.user_id)

    async def session_timeline(self, session_id: str) -> dict:
        """一个会话的完整回放：聊天气泡 + 每轮的执行过程事件。

        前端切换会话时用它重建"执行步骤"，否则那些步骤会随页面状态一起丢掉。
        草稿与逐字增量同样读取持久事件，历史回放与跨进程订阅使用同一来源。
        """
        turns = []
        live_task_id = ''
        live_after_event_id = 0
        for task in await self.store.session_tasks(session_id):
            state = task.get('state') or {}
            run_status = outcome_module.normalize_status(task['status'])
            record = self.tasks.get(task['task_id'])
            live = bool(record is not None and record.live) or run_status in \
                outcome_module.ACTIVE_RUN_STATUSES
            if live and not live_task_id:
                live_task_id = task['task_id']
            meta = await self.store.task_meta(task['task_id']) or {}
            run_id = str(meta.get('current_run_id') or '')
            events = await self.store.events(task['task_id'])
            if live and task['task_id'] == live_task_id:
                live_after_event_id = max(
                    (int(event.get('resume_after_event_id') or 0) for _, event in events), default=0)
            turns.append({
                'task_id': task['task_id'],
                # 切回历史会话时，前端要靠 run_id 才能给**正在跑的那一轮**补要求
                'run_id': run_id,
                'message': task['message'],
                # 库里存的是通用状态，回放时按 outcome 派生展示标签
                'status': outcome_module.label(run_status, state),
                'run_status': run_status,
                'created_at': task['created_at'],
                # 产物要能"切回来接着看"：视频/页面路径在这里给前端，
                # 否则一切会话就再也找不到入口（只有最后一条 finish 时才渲染过）
                'artifact': _artifact_view(state),
                'live': live,
                'asked_questions': [str(q) for q in (state.get('asked_questions') or [])
                                    if str(q).strip()],
                'clarification': str(state.get('clarification') or ''),
                'events': [event for _, event in events],
            })
        live_run_id = ''
        if live_task_id:
            meta = await self.store.task_meta(live_task_id) or {}
            live_run_id = str(meta.get('current_run_id') or '')
        return {'session_id': session_id,
                'messages': await self.store.messages(session_id),
                # 这个会话里还在跑的任务：前端切回来时要重新接上事件流
                'live_task_id': live_task_id,
                'live_run_id': live_run_id,
                'live_after_event_id': live_after_event_id,
                'turns': turns}

    async def prior_context(self, session_id: str) -> dict:
        """上一轮可以接过来用的东西：HTML 产物 + 已取得的正文。

        多轮里用户常说"把上面的内容整理成 HTML""部署到服务器"，这一轮自己不检索、
        也不重新渲染，全靠上一轮留下的材料。只接正文来源（不含纯知识作答），
        因为报告要引用来源，没有 URL 的来源没法在报告里成立。

        取的是**最近一轮**有产物的任务（DESC 的第一条），并把来源任务 ID 一并
        给出：以前这里只返回一个 artifact，"刚才那份"到底指哪一份全靠猜，
        界面上也没法解释。多份候选时调用方可以据此提示用户。
        """
        candidates = []
        for row in await self.store.tasks_of(session_id, limit=20):
            data = await self.store.task(row['task_id'])
            state = (data or {}).get('state') or {}
            if not isinstance(state, dict):
                continue
            artifact = state.get('artifact') or None
            documents = [d for d in (state.get('documents') or [])
                         if d.get('status') == 'success']
            if artifact or documents:
                candidates.append({'task_id': row['task_id'],
                                   'artifact': artifact, 'documents': documents,
                                   'created_at': row.get('created_at')})
        if not candidates:
            return {}
        picked = candidates[0]
        return {'artifact': picked['artifact'], 'documents': picked['documents'],
                'from_task': picked['task_id'],
                'candidates': len(candidates)}

    async def stop_session(self, session_id: str, message: str,
                     principal=None) -> TaskRecord:
        """把"暂停/停止"当成控制指令：掐掉在跑的任务，不启动新流程。

        界面上已经没有停止按钮，用户就靠说一句停下。若不拦下来，这句话会被当成
        普通任务送进规划器——想停反而跑起最重的一条链路。
        """
        tenant = getattr(principal, 'tenant_id', 'default')
        owner = getattr(principal, 'user_id', 'default')
        running = [task for task in self.tasks.values()
                   if task.session_id == session_id and task.live]
        for task in running:
            task._task.cancel()
        await self.store.append_message(session_id, 'user', message,
                                  tenant_id=tenant, owner_user_id=owner)
        record = TaskRecord(task_id=uuid.uuid4().hex[:12], session_id=session_id,
                            message=message, store=self.store,
                            tenant_id=tenant, owner_user_id=owner)
        await self.store.create_task(record.task_id, session_id, message)
        self.tasks[record.task_id] = record
        detail = ('已停止当前任务，没有重新开始检索。' if running
                  else '当前没有正在执行的任务。')
        await self.store.append_message(session_id, 'assistant', detail,
                                  tenant_id=tenant, owner_user_id=owner)
        await record.persist(status='cancelled', state={'status': 'cancelled'})
        await record.publish({'type': 'status', 'status': 'cancelled', 'detail': detail})
        record.close()
        return record
