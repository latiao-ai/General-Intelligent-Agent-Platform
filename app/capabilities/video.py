"""本地解说视频能力：读取 Skills，模型写分镜，MCP 执行配音与渲染。"""
import asyncio
import hashlib
import json
import os
import shutil
import wave
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from jsonschema import validate

from app.capabilities.base import ToolError
from app.capabilities.registry import tool
from app.config import PROJECT_ROOT, settings
from app.events import action, emit
from app.integrations import mcp_adapter, mcp_servers
from app.services import structured_call

CAPABILITY = 'video'
PROMPT_RULES = (
    '【本地动画解说】用户要求制作科普/知识动画解说视频时，优先只安排一步 '
    'make_local_explainer，topic 保留完整主题，duration_seconds 采用用户要求。'
    '此能力内部完成真实能力发现、按 Skills 写剧本分镜、中文配音、字幕、动画渲染与成片核验；'
    '不要再额外猜测 generate_script/generate_storyboard 或拼装底层 run_tool。'
    '不需要图像/视频生成服务密钥；模型仍使用本项目已配置的大模型。'
)


class Scene(BaseModel):
    title: str = Field(min_length=1, max_length=16)
    narration: str = Field(min_length=8, max_length=65)
    labels: list[str] = Field(min_length=1, max_length=3)
    visual: Literal['concept', 'spectrum', 'scatter', 'path', 'compare']


class Storyboard(BaseModel):
    title: str
    scenes: list[Scene] = Field(min_length=4, max_length=8)


def server_root():
    server = mcp_servers.by_id('openmontage')
    if server is None:
        raise ToolError('尚未配置 OpenMontage MCP Server')
    args = list(server.stdio_args)
    try:
        root = Path(args[args.index('--project-dir') + 1]).resolve()
    except (ValueError, IndexError):
        raise ToolError('OpenMontage 需要配置 --project-dir 绝对路径') from None
    if not (root / 'AGENT_GUIDE.md').is_file():
        raise ToolError('OpenMontage 项目目录或使用指南不存在')
    return server, root


async def command(*args, cwd=None, timeout=120):
    proc = await asyncio.create_subprocess_exec(*map(str, args), cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        raise ToolError(f'{Path(str(args[0])).name} 执行失败：{stderr.decode(errors="replace")[-1200:]}')
    return stdout.decode(errors='replace')


def voice_model(root):
    path = Path(os.getenv('LOCAL_VIDEO_VOICE_MODEL') or root / 'models/piper/zh_CN-huayan-medium.onnx').expanduser().resolve()
    if not path.is_file() or not Path(str(path) + '.json').is_file():
        raise ToolError('中文配音模型不完整：需安装 zh_CN-huayan-medium.onnx 及其 .json 文件')
    return path


@tool(name='make_local_explainer', label='制作动画解说视频', effect='write',
      key='local:video:explainer',
      description='制作完整的中文科普/知识动画解说视频，输出可播放下载的 MP4。自动写分镜、Piper 本地中文配音、Remotion 动画与字幕、FFmpeg 核验。',
      args={'type': 'object', 'properties': {
          'topic': {'type': 'string'},
          'duration_seconds': {'type': 'integer', 'minimum': 15, 'maximum': 120, 'default': 45}},
          'required': ['topic']})
async def make_local_explainer(args, state):
    server, root = server_root()
    model = voice_model(root)
    duration = int(args.get('duration_seconds', 45))
    if not 15 <= duration <= 120:
        raise ToolError('本地解说视频时长应在 15 到 120 秒之间')
    topic = str(args.get('topic') or '').strip()
    if not topic:
        raise ToolError('请提供视频主题')
    envbin = Path('/opt/anaconda3/envs/openmontage/bin')
    ffmpeg, ffprobe = envbin / 'ffmpeg', envbin / 'ffprobe'
    if not ffmpeg.is_file() or not ffprobe.is_file():
        raise ToolError('OpenMontage 环境缺少 FFmpeg / FFprobe')
    composer = root / 'remotion-composer'
    if not (composer / 'node_modules/.bin/remotion').is_file():
        raise ToolError('Remotion 依赖未安装，请先在 remotion-composer 执行 npm ci')
    folder = settings.artifact_dir / state['task_id'] / 'video'
    folder.mkdir(parents=True, exist_ok=True)
    emit(action('视频方案', '本地中文配音 + Remotion 720p 动画与字幕；无背景音乐，不使用付费图像/视频 API', '🎬'))
    async with mcp_adapter.connect_session(server) as session:
        async def rpc(name, arguments):
            result = await session.call_tool(name, arguments)
            return mcp_adapter.result_payload(result)

        caps = await rpc('list_capabilities', {})
        available = {t['name']: t for t in caps.get('tools', [])}
        for name in ['piper_tts', 'video_compose']:
            if name not in available:
                raise ToolError(f'服务端未提供实际所需工具 {name}')
        emit(action('已读取实际能力', 'Piper 配音与 Remotion 合成参数已核对', '🔎'))
        guides = []
        for file in ['skills/pipelines/explainer/script-director.md',
                     'skills/pipelines/explainer/scene-director.md']:
            guides.append((root / file).read_text(encoding='utf-8'))
        emit(action('编写剧本与分镜', f'按照 OpenMontage 阶段指南创作 {duration} 秒内容', '📝'))
        storyboard, usage = await structured_call(Storyboard,
            '你是中文科普动画导演，按以下项目 Skills 创作准确、易懂、可本地渲染的分镜。'
            '必须用中文；每个 narration 是实际要朗读的话，不能包含制作说明。'
            'visual 只能从本地动画原语中选：concept/compare 概念节点，spectrum 光谱，'
            'scatter 粒子散射，path 光程示意。labels 每条最多8个汉字。'
            '不编造数字或来源；光散射不能误写成蓝光被吸收。\n' + '\n'.join(guides),
            f'需求：{topic}\n时长：{duration}秒。4-8个镜头，旁白总计约{round(duration * 3.7)}个汉字，避免过长。'
            '结构：提问、解释、可视化、总结。每个镜头说明一个清楚的概念。',
            attempts=2)
        board = storyboard.model_dump()
        (folder / 'storyboard.json').write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding='utf-8')
        (folder / 'script.md').write_text('\n\n'.join(f'## {s.title}\n{s.narration}' for s in storyboard.scenes), encoding='utf-8')
        created = await rpc('create_project', {'title': f'{storyboard.title}-{state["task_id"][:8]}',
            'pipeline': 'animated-explainer', 'brief': topic})
        project_id = created['project_id']
        project = root / created['project_dir']
        audio_parts, lengths = [], []
        for i, scene in enumerate(storyboard.scenes):
            emit(action('生成中文旁白', f'镜头 {i + 1}/{len(storyboard.scenes)}：{scene.title}', '🎙️'))
            output = folder / f'voice-{i}.wav'
            inputs = {'text': scene.narration, 'model': str(model), 'output_path': str(output)}
            validate(inputs, available['piper_tts']['input_schema'])
            await rpc('run_tool', {'name': 'piper_tts', 'inputs': inputs, '_wait': True})
            with wave.open(str(output), 'rb') as f:
                lengths.append(f.getnframes() / f.getframerate())
            audio_parts.append(output)
        raw = folder / 'narration-raw.wav'
        with wave.open(str(raw), 'wb') as target:
            for i, path in enumerate(audio_parts):
                with wave.open(str(path), 'rb') as source:
                    if i == 0: target.setparams(source.getparams())
                    target.writeframes(source.readframes(source.getnframes()))
        speed = sum(lengths) / duration
        factors = []
        while speed > 2: factors.append(2); speed /= 2
        while speed < .5: factors.append(.5); speed /= .5
        factors.append(speed)
        audio = folder / 'narration.wav'
        await command(ffmpeg, '-y', '-i', raw, '-af', ','.join(f'atempo={s:.6f}' for s in factors) + f',apad,atrim=duration={duration}', audio)
        frames = [max(1, round(t / sum(lengths) * duration * 30)) for t in lengths]
        frames[-1] += duration * 30 - sum(frames)
        for scene, count in zip(board['scenes'], frames): scene['frames'] = count
        board['duration'] = duration
        offset, sections, scenes = 0, [], []
        for i, scene in enumerate(board['scenes']):
            start, end = offset / 30, (offset + scene['frames']) / 30
            sections.append({'id': f's{i+1}', 'text': scene['narration'], 'start_seconds': start, 'end_seconds': end})
            scenes.append({'id': f'scene-{i+1}', 'type': 'animation', 'description': scene['title'],
                'start_seconds': start, 'end_seconds': end, 'script_section_id': f's{i+1}'})
            offset += scene['frames']
        artifacts = {
            'script': {'version': '1.0', 'title': board['title'], 'total_duration_seconds': duration, 'sections': sections},
            'scene_plan': {'version': '1.0', 'scenes': scenes},
        }
        for name, content in artifacts.items():
            validate(content, json.loads((root / f'schemas/artifacts/{name}.schema.json').read_text()))
            (project / 'artifacts' / f'{name}.json').write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding='utf-8')
        props = folder / 'props.json'
        props.write_text(json.dumps(board, ensure_ascii=False), encoding='utf-8')
        # 入口放到 composer 树内，依赖解析和媒体地址均不依赖客户端当前目录。
        render_dir = composer / 'projects' / f'client-{state["task_id"]}'
        public = render_dir / 'public'
        public.mkdir(parents=True, exist_ok=True)
        entry = render_dir / 'index.tsx'
        shutil.copy2(PROJECT_ROOT / 'app/video/Explainer.tsx', entry)
        shutil.copy2(audio, public / 'narration.wav')
        output = folder / 'final.mp4'
        inputs = {'operation': 'render', 'output_path': str(output),
            'edit_decisions': {'render_runtime': 'remotion', 'composition_mode': 'atelier',
                'renderer_family': 'explainer-data',
                'bespoke': {'entry': str(entry), 'composition_id': 'LocalExplainer',
                    'props_path': str(props), 'public_dir': str(public), 'concurrency': 4,
                    'art_direction': '暖白底与深蓝文字；模型按主题选取光谱、散射、光程或概念图，逐帧驱动图形、粒子和字幕。'}},
            'asset_manifest': {'assets': []}}
        validate(inputs, available['video_compose']['input_schema'])
        (project / 'artifacts/edit_decisions.json').write_text(json.dumps(inputs['edit_decisions'], ensure_ascii=False), encoding='utf-8')
        emit(action('渲染动画视频', f'{duration} 秒 · 中文旁白及同步字幕 · 1280×720', '🎞️'))
        await rpc('run_tool', {'name': 'video_compose', 'inputs': inputs, '_wait': True})
        probe = json.loads(await command(ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', output))
        streams = probe['streams']
        if not any(s['codec_type'] == 'video' for s in streams) or not any(s['codec_type'] == 'audio' for s in streams):
            raise ToolError('成片核验失败：缺少视频或声音轨道')
        actual = float(probe['format']['duration'])
        if abs(actual - duration) > .2:
            raise ToolError('成片时长核验失败')
        emit(action('成片核验通过', f'{actual:.1f} 秒 · 画面与声音轨道完整', '✅'))
    artifact = {'artifact_id': state['task_id'], 'kind': 'video', 'path': str(output),
        'mime': 'video/mp4', 'title': storyboard.title, 'duration_seconds': actual,
        'size_bytes': output.stat().st_size, 'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}
    return {'artifact': artifact, 'answer': f'已生成《{storyboard.title}》，时长 {actual:.0f} 秒，包含中文旁白、动画和字幕，可在下方播放或下载。',
        'usage': list(state.get('usage', [])) + [{'stage': 'video_storyboard', 'model': usage}], 'status': 'rendered'}
