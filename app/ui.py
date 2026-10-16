"""聊天式前端：实时展示智能体的执行过程，并支持中途停止后追加要求。

Streamlit 的执行模型是"脚本重跑"，脚本执行期间不响应交互，所以任务放在
app.session.Session 的后台线程里跑；这里只做两件事：
1. 用 st.fragment(run_every=...) 轮询事件队列，呈现"思考过程"；
2. 把用户的新消息交给会话，开启下一轮（带上历史，可修正方向）。
"""
import asyncio
import time
from pathlib import Path
import streamlit as st
from app.config import settings
from app.main import STAGE_LABELS, deploy_task, format_timings
from app.session import TERMINAL_STATUS, Session

st.set_page_config(page_title='网页研究报告', layout='wide')
st.title('网页研究与 HTML 报告')

# 逐键初始化：任意一个键缺失都补上，不依赖"session 在不在"这一个条件。
st.session_state.setdefault('session', Session())
st.session_state.setdefault('events', [])
st.session_state.setdefault('stages', [])
st.session_state.setdefault('streaming', False)
session: Session = st.session_state.session


# ---------------- 事件渲染 ----------------

def render_event(event: dict) -> None:
    kind = event.get('type')
    if kind == 'thought':
        st.markdown(f"💭 {event.get('text', '')}")
    elif kind == 'action':
        icon = event.get('icon') or '•'
        detail = event.get('detail') or ''
        tail = f"：{detail}" if detail else ''
        st.markdown(f"{icon} {event.get('label', '')}{tail}")
    elif kind == 'result':
        st.success(f"部署完成：{event.get('public_url', '')}")
    elif kind == 'status':
        if event.get('status') == 'running':
            st.caption('执行中…')
        elif event.get('detail'):
            st.info(event['detail'])


def consume() -> None:
    for event in session.drain():
        kind = event.get('type')
        if kind == 'stage_start':
            st.session_state.stages.append({
                'stage': event.get('stage'), 'label': event.get('label', event.get('stage')),
                'at': event.get('at', time.time()),
                'done': False, 'seconds': None, 'status': '',
            })
            continue
        if kind == 'stage_end':
            # 补搜会让同一阶段出现多次，从后往前关闭最后一个未完成项。
            for item in reversed(st.session_state.stages):
                if item['stage'] == event.get('stage') and not item['done']:
                    item.update(done=True, seconds=event.get('seconds'),
                                status=event.get('status') or '完成')
                    break
            continue
        st.session_state.events.append(event)


def stage_rows() -> list[dict]:
    """阶段耗时。未完成的按"现在 - 开始时刻"实时累加。"""
    now = time.time()
    rows = []
    for item in st.session_state.stages:
        seconds = item['seconds'] if item['done'] else round(now - item['at'], 2)
        rows.append({
            '阶段': item['label'],
            '耗时(秒)': seconds,
            '状态': item['status'] if item['done'] else '进行中…',
        })
    return rows


def panel_body() -> None:
    consume()
    rows = stage_rows()
    with st.container(border=True):
        st.caption('阶段耗时' + ('（进行中，每 0.5 秒刷新）' if session.running else ''))
        if rows:
            st.dataframe(rows, width='stretch', hide_index=True)
            st.caption(f"合计 {sum(r['耗时(秒)'] for r in rows):.2f} 秒")
        else:
            st.caption('（尚未开始）')
    with st.container(border=True):
        st.caption('智能体执行过程' + ('（进行中）' if session.running else ''))
        if not st.session_state.events:
            st.caption('（尚无动作）')
        for event in st.session_state.events:
            render_event(event)
        if session.running and st.button('停止', key='stop'):
            session.cancel()
            st.rerun(scope='app')
    # 任务结束后整页重跑一次，让最终结果区（下载/部署/耗时）出现。
    if not session.running and st.session_state.get('streaming'):
        st.session_state['streaming'] = False
        st.rerun(scope='app')


# ---------------- 对话历史 ----------------

for message in session.messages:
    with st.chat_message(message['role']):
        st.write(message['content'])


# ---------------- 最终结果区 ----------------

state = session.result
if state:
    artifact = state.get('artifact')
    if artifact:
        st.download_button(
            '下载 HTML 报告', Path(artifact['html_path']).read_bytes(),
            file_name='index.html', mime='text/html',
        )
        st.caption('本地文件：' + artifact['html_path'])

    deployment = state.get('deployment') or {}
    if artifact and deployment.get('status') != 'success':
        if settings.deploy_enabled:
            if st.button('部署到服务器', type='primary'):
                started = time.perf_counter()
                try:
                    with st.spinner('正在部署到远端服务器……'):
                        result = asyncio.run(deploy_task(state['task_id']))
                    state['deployment'] = result
                    state['status'] = 'deployed'
                    state.setdefault('timings', []).append({
                        'stage': 'manual_deploy',
                        'seconds': round(time.perf_counter() - started, 2),
                        'status': 'deployed',
                    })
                    session.result = state
                    st.success('部署成功。')
                except Exception as exc:
                    st.error(f'部署失败（{type(exc).__name__}）：{exc}')
        else:
            st.button('部署到服务器', disabled=True)
            st.info('当前 .env 中 DEPLOY_ENABLED=false，部署功能已关闭。')

    deployment = state.get('deployment') or {}
    if deployment.get('public_url'):
        st.link_button('打开部署后的报告', deployment['public_url'])
        st.caption('访问地址：' + deployment['public_url'])
        if deployment.get('deployment_id'):
            st.caption('站点 ID：' + str(deployment['deployment_id']))
    elif deployment.get('status') == 'unknown':
        st.warning('部署超时，结果未知；请先查询服务端状态再决定是否重试。')
    elif deployment.get('status') == 'needs_check':
        st.warning('部署调用或返回校验失败，本地文件已保留；请核对服务端状态。')

    rows = [
        {'阶段': STAGE_LABELS.get(item.get('stage'), item.get('stage')),
         '耗时(秒)': item.get('seconds'),
         '状态': item.get('status')}
        for item in (state.get('timings') or [])
    ]
    if state['status'] in {'failed', 'deploy_needs_check'}:
        st.warning('任务未完整完成，请根据任务 ID 检查运行记录和服务端状态。')


# ---------------- 实时过程面板 ----------------

if session.running:
    st.fragment(panel_body, run_every=0.5, key='live_panel')()
else:
    panel_body()


# ---------------- 输入 ----------------

placeholder = ('输入需求，例如：整理 SQLite 的适用场景，生成中文报告；'
               '生成后想直接发布就说"…然后部署到服务器"')
if prompt := st.chat_input(placeholder):
    if session.running:
        st.warning('当前任务还在执行，请先点「停止」再补充要求。')
    else:
        st.session_state.events = []
        st.session_state.stages = []
        st.session_state.streaming = True
        session.start(prompt)
        st.rerun()

if session.messages:
    st.caption('追加要求时会带上之前的对话，例如先问"整理还珠格格影视资料"，'
               '再补一句"只要第一部第三集"。')
