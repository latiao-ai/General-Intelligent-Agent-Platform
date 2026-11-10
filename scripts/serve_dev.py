"""一键启动中台（后端 + Worker + 可选前端代理），默认对局域网开放。

    python -m scripts.serve_dev                     # 后端 0.0.0.0:8002 + Worker
    python -m scripts.serve_dev --with-frontend     # 再起 8001 前端代理（需 BeeFTV）
    python -m scripts.serve_dev --migrate           # 启动前先跑一次数据库迁移
    python -m scripts.serve_dev --local             # 只绑 127.0.0.1，不开局域网
    python -m scripts.serve_dev --reload            # 后端改代码自动重启（开发用）

端口被占用时（例如上一次没退干净的进程还占着 8001/8002）：

    python -m scripts.serve_dev --with-frontend --force       # 结束本项目残留进程后重启
    python -m scripts.serve_dev --with-frontend --auto-port   # 不动别人的进程，自动换空闲端口
    python -m scripts.serve_dev --with-frontend --force-any   # 连非本项目的占用进程也结束

几点说明：

- **中台自带界面**：后端根路径 `/` 就是聊天界面，所以日常只需要这个脚本，
  「独立前端」只在要嵌进 BeeFTV 画布时才需要（`--with-frontend`）。
- **Worker 不监听端口**：它从 PostgreSQL 里领任务。不起它的话，会话摘要不会
  生成、MCP 长任务也不会推进。`--force` 会顺手清掉上一次残留的 Worker，
  否则两个 Worker 抢同一批任务。
- `--force` 只结束「本项目自己的进程」（命令行里带 scripts.serve_api /
  serve_frontend / serve_dev / run_worker，且工作目录是本仓库）；遇到别人的
  进程会拒绝并打印 pid 与命令，需要的话用 `--force-any`（或换端口）。
- `--auto-port` 被动换端口：后端从 `--port`、前端从 `--frontend-port` 往后找
  第一个空闲端口，结束时打印实际地址。它不会结束任何进程。
- `--lan` 会关闭 Secure Cookie、把本机局域网地址加进 `AUTH_ALLOWED_ORIGINS`，
  属于联调用法；生产请用 HTTPS 反向代理（`serve_api` 会拒绝在
  `APP_ENV=production` 下开 `--lan`）。

Ctrl+C 一次收工：所有子进程一起退出。
"""
import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# 本项目的常驻进程模块名：--force 只认这些（配合工作目录判断，不误伤别人的服务）。
_MODULES = ('scripts.serve_api', 'scripts.serve_frontend', 'scripts.serve_dev',
            'scripts.serve_skillhub', 'scripts.run_worker')


def lan_address():
    """本机局域网地址。见 scripts/lanaddr.py（避开代理 TUN 的 198.18/15）。"""
    from scripts.lanaddr import lan_address as _lan
    return _lan()


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def listener_pids(port: int) -> list:
    """监听该端口的进程号。没有 lsof（或它超时）时返回空列表，调用方按“占用者未知”处理。"""
    if not shutil.which('lsof'):
        return []
    try:
        out = subprocess.run(['lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-t'],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({int(x) for x in out.split() if x.strip().isdigit()})


def process_command(pid: int) -> str:
    """进程完整命令行，取不到就返回空串。"""
    try:
        out = subprocess.run(['ps', '-o', 'command=', '-p', str(pid)],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ''
    return out.strip()


def process_cwd(pid: int) -> str:
    """进程工作目录，取不到就返回空串。"""
    if not shutil.which('lsof'):
        return ''
    try:
        out = subprocess.run(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ''
    for line in out.splitlines():
        if line.startswith('n'):
            return line[1:].strip()
    return ''


def is_project_process(pid: int) -> bool:
    """是不是本项目起的常驻进程：解释器 + 本项目模块 + 仓库目录，三者同时满足。"""
    cmd = process_command(pid)
    if not cmd:
        return False
    argv = cmd.split()
    if not Path(argv[0]).name.startswith('python'):
        return False
    if not any(module in cmd for module in _MODULES) and str(_ROOT) not in cmd:
        return False
    return process_cwd(pid) == str(_ROOT)


def wait_port_free(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_in_use(port):
            return True
        time.sleep(0.2)
    return not port_in_use(port)


def stop_process(pid: int, grace: float = 5.0) -> bool:
    """先 SIGTERM，宽限内没退出再 SIGKILL。返回进程是否已经不在。"""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            print(f'[serve_dev] 没有权限结束 pid {pid}，请手动处理。', file=sys.stderr)
            return False
        deadline = time.time() + grace
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                break
            time.sleep(0.1)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def project_processes(exclude=()) -> list:
    """扫出本仓库残留的常驻进程（按 pid+命令行），用于 --force 清理。"""
    try:
        out = subprocess.run(['ps', '-axo', 'pid=,command='],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        pid_text, _, cmd = line.strip().partition(' ')
        if not pid_text.isdigit() or int(pid_text) in exclude:
            continue
        pid = int(pid_text)
        argv = cmd.split()
        # 只认解释器进程本身：避免把命令行里带 "scripts.serve_api" 的 shell 一起结束。
        if not argv or not Path(argv[0]).name.startswith('python'):
            continue
        if not any(module in cmd for module in _MODULES):
            continue
        if process_cwd(pid) != str(_ROOT):
            continue
        found.append((pid, cmd.strip()))
    return found


def stop_leftovers(exclude=()) -> None:
    """结束上一次没退干净的 Worker（它不占端口，但会和新 Worker 抢任务）。"""
    for pid, cmd in project_processes(exclude):
        print(f'[serve_dev] 结束残留进程：pid {pid} {cmd}', flush=True)
        stop_process(pid)


def free_port(port: int, what: str, force_any: bool = False) -> bool:
    """结束占用端口的进程。只动本项目的进程，除非显式 force_any。"""
    pids = listener_pids(port)
    if not pids:
        print(f'[serve_dev] 端口 {port}（{what}）被占用，但认不出占用者（缺 lsof）；'
              f'请手动结束，或用 --auto-port 换端口。', file=sys.stderr)
        return False
    for pid in pids:
        cmd = process_command(pid) or '?'
        if not force_any and not is_project_process(pid):
            print(f'[serve_dev] 端口 {port}（{what}）被非本项目进程占用：\n'
                  f'           pid {pid}  {cmd}\n'
                  f'           确认可以结束后加 --force-any；否则用 --auto-port 或 --port/--frontend-port 换端口。',
                  file=sys.stderr)
            return False
        print(f'[serve_dev] 结束占用 {port}（{what}）的进程：pid {pid}  {cmd}', flush=True)
        stop_process(pid)
    if wait_port_free(port):
        return True
    print(f'[serve_dev] 端口 {port}（{what}）仍未释放。', file=sys.stderr)
    return False


def first_free_port(start: int, taken=(), span: int = 20):
    for port in range(start, start + span):
        if port in taken or port_in_use(port):
            continue
        return port
    return None


def resolve_ports(wanted, auto_port: bool):
    """auto_port 时把被占用的端口往后挪到空闲端口；返回 [(port, what), ...]，放弃时返回 None。"""
    taken, resolved = set(), []
    for port, what in wanted:
        if port in taken or port_in_use(port):
            if not auto_port:
                taken.add(port)
                resolved.append((port, what))
                continue
            new_port = first_free_port(port + 1, taken)
            if new_port is None:
                print(f'[serve_dev] {what}端口从 {port} 往后 20 个都被占用，放弃。', file=sys.stderr)
                return None
            print(f'[serve_dev] {what}端口 {port} 已被占用，改用 {new_port}', flush=True)
            port = new_port
        taken.add(port)
        resolved.append((port, what))
    return resolved


def spawn(label, argv, env=None):
    print(f'[serve_dev] 启动 {label}: {" ".join(argv[2:])}', flush=True)
    return subprocess.Popen(argv, cwd=str(_ROOT), env=env)


def main():
    parser = argparse.ArgumentParser(description='一键启动中台（后端 + Worker + 可选前端）')
    parser.add_argument('--port', type=int, default=8002, help='后端端口，默认 8002')
    parser.add_argument('--frontend-port', type=int, default=8001, help='前端代理端口，默认 8001')
    parser.add_argument('--skillhub-port', type=int, default=8010, help='SkillHub 端口，默认 8010')
    parser.add_argument('--with-frontend', action='store_true',
                        help='同时启动 8001 前端联调代理（需要 BeeFTV 在 8003/8004）')
    parser.add_argument('--no-worker', action='store_true', help='不启动 Worker')
    parser.add_argument('--no-skillhub', action='store_true', help='不启动 SkillHub')
    parser.add_argument('--migrate', action='store_true', help='启动前先执行数据库迁移')
    parser.add_argument('--local', action='store_true', help='只绑 127.0.0.1，不对局域网开放')
    parser.add_argument('--reload', action='store_true', help='后端改代码自动重启（开发用）')
    parser.add_argument('--force', action='store_true',
                        help='端口被本项目残留进程占用时，结束它再重启（含残留 Worker）')
    parser.add_argument('--force-any', action='store_true',
                        help='连同非本项目的占用进程一起结束（危险，确认过再用）')
    parser.add_argument('--auto-port', action='store_true',
                        help='不结束任何进程，端口被占用时自动往后找空闲端口')
    args = parser.parse_args()

    if args.force_any:
        args.force = True
    if args.force and args.auto_port:
        parser.error('--force 与 --auto-port 语义冲突（一个清场、一个绕开），请二选一')

    lan = not args.local
    api_port, fe_port = args.port, args.frontend_port

    if args.force:
        stop_leftovers(exclude={os.getpid(), os.getppid()})

    wanted = [(api_port, '后端')]
    if not args.no_skillhub:
        wanted.append((args.skillhub_port, 'SkillHub'))
    if args.with_frontend:
        wanted.append((fe_port, '前端代理'))
    if args.force:
        for port, what in wanted:
            if port_in_use(port) and not free_port(port, what, args.force_any):
                return 2

    resolved = resolve_ports(wanted, args.auto_port)
    if resolved is None:
        return 2
    api_port = resolved[0][0]
    index = 1
    skillhub_port = args.skillhub_port
    if not args.no_skillhub:
        skillhub_port = resolved[index][0]
        index += 1
    if args.with_frontend:
        fe_port = resolved[index][0]

    busy = [f'{port}（{what}）' for port, what in resolved if port_in_use(port)]
    if busy:
        print(f'[serve_dev] 端口已被占用：{"、".join(busy)}\n'
              f'           加 --force 结束本项目残留进程后重启，--auto-port 自动换端口，\n'
              f'           或直接指定 --port / --skillhub-port / --frontend-port。',
              file=sys.stderr)
        return 2

    api_cmd = [sys.executable, '-m', 'scripts.serve_api', '--port', str(api_port)]
    api_cmd += ['--lan'] if lan else []
    api_cmd += ['--reload'] if args.reload else []

    api_env = os.environ.copy()
    configured_url = os.getenv('SKILLHUB_URL', '').strip()
    if not configured_url:
        try:
            from dotenv import dotenv_values
            configured_url = str(dotenv_values(_ROOT / '.env').get('SKILLHUB_URL') or '').strip()
        except Exception:                              # noqa: BLE001
            configured_url = ''
    if not configured_url and not args.no_skillhub:
        api_env['SKILLHUB_URL'] = f'http://127.0.0.1:{skillhub_port}/v1/skills'

    if args.migrate:
        print('[serve_dev] 执行数据库迁移 …', flush=True)
        done = subprocess.run([sys.executable, '-m', 'scripts.migrate'], cwd=str(_ROOT))
        if done.returncode != 0:
            print('[serve_dev] 迁移失败，已中止。', file=sys.stderr)
            return done.returncode

    children = []
    try:
        if not args.no_skillhub:
            children.append(('SkillHub', spawn('SkillHub',
                                               [sys.executable, '-m', 'scripts.serve_skillhub',
                                                '--port', str(skillhub_port)])))
        children.append(('后端', spawn('后端', api_cmd, env=api_env)))
        if not args.no_worker:
            children.append(('Worker', spawn('Worker',
                                             [sys.executable, '-m', 'scripts.run_worker'])))
        if args.with_frontend:
            fe = [sys.executable, '-m', 'scripts.serve_frontend',
                  '--port', str(fe_port),
                  '--backend', f'http://127.0.0.1:{api_port}']
            fe += ['--lan'] if lan else []
            children.append(('前端代理', spawn('前端代理', fe)))

        address = lan_address() if lan else None
        print('\n[serve_dev] 启动完成，按 Ctrl+C 一起停止：', flush=True)
        print(f'  中台界面/接口    http://127.0.0.1:{api_port}/', flush=True)
        if not args.no_skillhub:
            print(f'  SkillHub         http://127.0.0.1:{skillhub_port}/v1/health', flush=True)
        if lan and address:
            print(f'  局域网访问       http://{address}:{api_port}/', flush=True)
        elif lan:
            print(f'  局域网访问       http://<本机IP>:{api_port}/', flush=True)
        if args.with_frontend:
            print(f'  前端代理         http://127.0.0.1:{fe_port}/'
                  f'（需 BeeFTV 8003/8004）', flush=True)
            if lan and address:
                print(f'  前端代理(局域网)  http://{address}:{fe_port}/', flush=True)
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
