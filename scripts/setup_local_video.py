"""安装并检查零密钥中文解说视频依赖；在主项目的 demo 环境执行。"""
import subprocess
import hashlib
from pathlib import Path

from app.capabilities.video import server_root


def run(*args, cwd=None):
    subprocess.run(list(map(str, args)), cwd=cwd, check=True)


def install_voice(voices):
    """校验官方文件摘要，断网后继续下载，完成前不暴露为可用模型。"""
    model = voices/'zh_CN-huayan-medium.onnx'
    expected = '9929917bf8cabb26fd528ea44d3a6699c11e87317a14765312420be230be0f3d'
    def complete(path):
        return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected
    base = 'https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/'
    if not complete(model):
        partial = model.with_suffix('.onnx.part')
        if model.exists():
            model.replace(partial)
        for attempt in range(2):
            run('curl', '--fail', '--location', '--retry', '5', '--retry-delay', '2',
                '--connect-timeout', '20', '--max-time', '1200', '--continue-at', '-',
                '--output', partial, base + model.name + '?download=true')
            if complete(partial):
                partial.replace(model)
                break
            partial.unlink(missing_ok=True)
        else:
            raise RuntimeError('中文语音模型摘要校验失败，请检查下载连接')
    config = Path(str(model) + '.json')
    if not config.is_file() or not config.stat().st_size:
        partial = Path(str(config) + '.part')
        run('curl', '--fail', '--location', '--retry', '5', '--connect-timeout', '20',
            '--max-time', '120', '--output', partial, base + config.name + '?download=true')
        partial.replace(config)


def main():
    server, root = server_root()
    conda = Path('/opt/anaconda3/bin/conda')
    runtime = Path('/opt/anaconda3/envs/openmontage/bin')
    run(conda, 'run', '--no-capture-output', '-n', 'openmontage', 'python', '-m',
        'pip', 'install', '-r', root/'requirements.txt', '-e', f'{root}[mcp]', 'mcp>=1,<2', 'piper-tts')
    if not (runtime/'ffmpeg').exists() or not (runtime/'ffprobe').exists():
        run(conda, 'install', '-n', 'openmontage', 'ffmpeg', '-y')
    composer = root/'remotion-composer'
    if not (composer/'node_modules/.bin/remotion').exists():
        run('npm', 'ci', cwd=composer)
    voices = root/'models/piper'
    voices.mkdir(parents=True, exist_ok=True)
    install_voice(voices)
    if not Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome').exists():
        run('npx', '--no-install', 'remotion', 'browser', 'ensure', cwd=composer)
    # 真正加载模型并发声，能同时发现模型不完整、配置缺失和音素化依赖问题。
    sample = voices/'installation-check.wav'
    subprocess.run([str(runtime/'piper'), '--model', str(voices/'zh_CN-huayan-medium.onnx'),
                    '--output_file', str(sample)], input='天空为什么是蓝色的？这是中文语音依赖检查。',
                   text=True, check=True)
    print('中文配音模型、FFmpeg、Remotion 与浏览器检查完成。')


if __name__ == '__main__':
    main()
