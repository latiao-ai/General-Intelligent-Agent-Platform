"""启动 FastAPI + SSE 服务。

    python -m scripts.serve_api                    # 默认 127.0.0.1:8002
    python -m scripts.serve_api --port 9000        # 换端口
    python -m scripts.serve_api --reload           # 开发模式，改代码自动重启

启动后浏览器打开 http://127.0.0.1:8002/ 即可使用聊天界面。
"""
import argparse
import os
import socket

import uvicorn
# 直接跑脚本时（python scripts/xxx.py）sys.path[0] 是 scripts/，仓库根与 src/ 都不在其中，
# 导入 webpage_agent.* 会失败。这里显式补上，让"直接跑"和"python -m scripts.xxx"都能用。
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT / 'src'))


def lan_address():
    """Ask the routing table for the primary IPv4 address; sends no packets."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('192.0.2.1', 9))
            return sock.getsockname()[0]
    except OSError:
        return None


def main():
    parser = argparse.ArgumentParser(description='启动网页研究报告 API 服务')
    parser.add_argument('--host', default=None)
    parser.add_argument('--lan', action='store_true', help='通过 HTTP 向局域网开放，仅供开发联调')
    parser.add_argument('--port', type=int, default=8002)
    parser.add_argument('--reload', action='store_true', help='开发模式：改代码自动重启')
    args = parser.parse_args()
    if args.lan:
        if args.host and args.host != '0.0.0.0':
            parser.error('--lan 与指定的 --host 冲突')
        if os.getenv('APP_ENV', '').strip().lower() == 'production':
            parser.error('生产环境请通过 HTTPS 反向代理访问，不使用 --lan HTTP 联调模式')
        from dotenv import load_dotenv
        load_dotenv(_ROOT / '.env')
        if os.getenv('APP_ENV', '').strip().lower() == 'production':
            parser.error('生产环境不能使用 --lan')
        address = lan_address()
        origins = {x.strip() for x in os.getenv('AUTH_ALLOWED_ORIGINS', '').split(',') if x.strip()}
        origins.update({f'http://localhost:{args.port}', f'http://127.0.0.1:{args.port}'})
        if address:
            origins.add(f'http://{address}:{args.port}')
        os.environ['AUTH_ALLOWED_ORIGINS'] = ','.join(sorted(origins))
        os.environ['AUTH_COOKIE_SECURE'] = 'false'
        print('局域网开发联调模式：保留现有登录认证，使用 HTTP Cookie。', flush=True)
        if address:
            print(f'同一局域网访问：http://{address}:{args.port}', flush=True)
        else:
            print(f'请用本机局域网 IP 访问：http://<本机IP>:{args.port}', flush=True)
    uvicorn.run('webpage_agent.api.routes:app', host='0.0.0.0' if args.lan else (args.host or '127.0.0.1'),
                port=args.port, reload=args.reload)


if __name__ == '__main__':
    main()
