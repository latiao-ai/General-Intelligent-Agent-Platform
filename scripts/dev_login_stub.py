"""本地开发用的「平台账号」登录桩：让中台的账号密码登录入口有地方校验。

**这不是产品代码。** 中台的 `/api/auth/platform-login` 会把账号密码交给上游平台
自己的 `/auth/login` 校验（中台不存、不校验密码）。本地没有真实平台时，这个桩
顶替上游：任意非空账号密码都放行，并固定返回同一个平台用户（`dev-owner`）。

配合 `scripts/serve_dev_login.sh` 使用：

    python -m scripts.dev_login_stub --port 8099      # 单独起桩
    bash scripts/serve_dev_login.sh                   # 桩 + 中台一起起

桩返回的 `dev-owner` 需要在库里绑定到本机原来的单用户（user_id='default'，
tenant_id='default'），否则登录后会进一个空账号、看不到已有的会话和任务。
绑定只需做一次（可重复执行）：

    python -m scripts.dev_login_bind --name 浩南

对外只提供两个地址：

    POST /api/auth/login   {"username": "...", "password": "..."}
        200 → {"data": {"user": {"id": "dev-owner", "username": "...",
                                 "status": "active", "role": "member"}}}
        401 → 账号或密码为空
    GET  /api/auth/login   说明页（浏览器直接打开时看）
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 固定的平台用户标识。改这里就要同步改 deploy/sql/dev-login-bind.sql，
# 否则登录后会落到一个新账号上。
DEV_SUBJECT = 'dev-owner'

PAGE = """<!doctype html>
<meta charset="utf-8"><title>本地登录桩</title>
<style>body{font:15px/1.7 system-ui,-apple-system,"PingFang SC",sans-serif;
max-width:640px;margin:40px auto;padding:0 20px;color:#1b2430}
code{background:#f2f4f7;padding:1px 5px;border-radius:4px}
.muted{color:#6b7684;font-size:13px}</style>
<h1>本地登录桩（开发用）</h1>
<p>这里只做一件事：让中台的「登录中台 → 账号密码」有上游可校验。</p>
<p class="muted">接口：<code>POST /api/auth/login</code>，任意非空账号密码都放行，
固定返回平台用户 <code>{subject}</code>。</p>
<p class="muted">中台地址见启动它的那个终端；登录入口在左下角账号卡片。</p>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):        # 安静一点
        pass

    def _json(self, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        body = PAGE.format(subject=DEV_SUBJECT).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.rstrip('/') != '/api/auth/login':
            self._json(404, {'detail': 'not found'})
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
            payload = json.loads(self.rfile.read(length) or b'{}')
        except (ValueError, TypeError):
            self._json(400, {'detail': 'invalid json'})
            return
        username = str(payload.get('username') or '').strip()
        password = str(payload.get('password') or '')
        if not username or not password:
            # 与真实平台一致：账号或密码缺失按"不正确"回 401，不泄露哪个错了
            self._json(401, {'detail': '账号或密码不正确'})
            return
        self._json(200, {'data': {'user': {
            'id': DEV_SUBJECT,
            'username': username,
            'status': 'active',
            'role': 'member',
        }}})


def main() -> int:
    parser = argparse.ArgumentParser(description='本地开发用平台账号登录桩')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8099)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'[login-stub] http://{args.host}:{args.port}/api/auth/login  '
          f'→ 固定平台用户 {DEV_SUBJECT}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n[login-stub] 退出')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
