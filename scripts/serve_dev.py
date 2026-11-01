"""一键启动中台（后端 + Worker + 可选前端代理），默认对局域网开放。

    python -m scripts.serve_dev                     # 后端 0.0.0.0:8002 + Worker
    python -m scripts.serve_dev --with-frontend     # 再起 8001 前端代理（需 BeeFTV）
    python -m scripts.serve_dev --migrate           # 启动前先跑一次数据库迁移
    python -m scripts.serve_dev --local             # 只绑 127.0.0.1，不开局域网
    python -m scripts.serve_dev --reload            # 后端改代码自动重启（开发用）

几点说明：

- **中台自带界面**：后端根路径 `/` 就是聊天界面，所以日常只需要这个脚本，
  「独立前端」只在要嵌进 BeeFTV 画布时才需要（`--with-frontend`）。
- **Worker 不监听端口**：它从 PostgreSQL 里领任务。不起它的话，会话摘要不会
  生成、MCP 长任务也不会推进。
- `--lan` 会关闭 Secure Cookie、把本机局域网地址加进 `AUTH_ALLOWED_ORIGINS`，
  属于联调用法；生产请用 HTTPS 反向代理（`serve_api` 会拒绝在
  `APP_ENV=production` 下开 `--lan`）。

Ctrl+C 一次收工：所有子进程一起退出。
"""
import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def lan_address():
    """本机局域网地址。见 scripts/lanaddr.py（避开代理 TUN 的 198.18/15）。"""
    from scripts.lanaddr import lan_address as _lan
    return _lan()


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def spawn(label, argv):
    print(f'[serve_dev] 启动 {label}: {" ".join(argv[2:])}', flush=True)
    return subprocess.Popen(argv, cwd=str(_ROOT))


def main():
    parser = argparse.ArgumentParser(description='一键启动中台（后端 + Worker + 可选前端）')
    parser.add_argument('--port', type=int, default=8002, help='后端端口，默认 8002')
    parser.add_argument('--frontend-port', type=int, default=8001, help='前端代理端口，默认 8001')
    parser.add_argument('--with-frontend', action='store_true',
                        help='同时启动 8001 前端联调代理（需要 BeeFTV 在 8003/8004）')
    parser.add_argument('--no-worker', action='store_true', help='不启动 Worker')
    parser.add_argument('--migrate', action='store_true', help='启动前先执行数据库迁移')
    parser.add_argument('--local', action='store_true', help='只绑 127.0.0.1，不对局域网开放')
    parser.add_argument('--reload', action='store_true', help='后端改代码自动重启（开发用）')
    args = parser.parse_args()

    lan = not args.local
    api_cmd = [sys.executable, '-m', 'scripts.serve_api', '--port', str(args.port)]
    api_cmd += ['--lan'] if lan else []
    api_cmd += ['--reload'] if args.reload else []

    ports = {args.port: '后端'}
    if args.with_frontend:
        ports[args.frontend_port] = '前端代理'
    busy = [f'{port}（{what}）' for port, what in ports.items() if port_in_use(port)]
    if busy:
        print(f'[serve_dev] 端口已被占用：{"、".join(busy)}\n'
              f'           先停掉占用的进程，或用 --port / --frontend-port 换端口。',
              file=sys.stderr)
        return 2

    if args.migrate:
        print('[serve_dev] 执行数据库迁移 …', flush=True)
        done = subprocess.run([sys.executable, '-m', 'scripts.migrate'], cwd=str(_ROOT))
        if done.returncode != 0:
            print('[serve_dev] 迁移失败，已中止。', file=sys.stderr)
            return done.returncode

    children = []
    try:
        children.append(('后端', spawn('后端', api_cmd)))
        if not args.no_worker:
            children.append(('Worker', spawn('Worker',
                                             [sys.executable, '-m', 'scripts.run_worker'])))
        if args.with_frontend:
            fe = [sys.executable, '-m', 'scripts.serve_frontend',
                  '--port', str(args.frontend_port),
                  '--backend', f'http://127.0.0.1:{args.port}']
            fe += ['--lan'] if lan else []
            children.append(('前端代理', spawn('前端代理', fe)))

        address = lan_address() if lan else None
        print('\n[serve_dev] 启动完成，按 Ctrl+C 一起停止：', flush=True)
        print(f'  中台界面/接口    http://127.0.0.1:{args.port}/', flush=True)
        if lan and address:
            print(f'  局域网访问       http://{address}:{args.port}/', flush=True)
        elif lan:
            print(f'  局域网访问       http://<本机IP>:{args.port}/', flush=True)
        if args.with_frontend:
            print(f'  前端代理         http://127.0.0.1:{args.frontend_port}/'
                  f'（需 BeeFTV 8003/8004）', flush=True)
            if lan and address:
                print(f'  前端代理(局域网)  http://{address}:{args.frontend_port}/', flush=True)
        print('', flush=True)

        while True:
            time.sleep(0.5)
            for label, proc in children:
                code = proc.poll()
                if code is not None:
                    print(f'[serve_dev] {label} 已退出（code={code}），停止其余进程。',
                          file=sys.stderr, flush=True)
                    return code or 1
    except KeyboardInterrupt:
        print('\n[serve_dev] 收到 Ctrl+C，正在停止 …', flush=True)
        return 0
    finally:
        for label, proc in children:
            if proc.poll() is None:
                proc.terminate()
        deadline = time.time() + 10
        for label, proc in children:
            try:
                proc.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                print(f'[serve_dev] {label} 未按时退出，强制结束。', file=sys.stderr)
                proc.kill()


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    sys.exit(main())
