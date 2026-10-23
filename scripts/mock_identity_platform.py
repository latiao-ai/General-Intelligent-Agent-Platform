"""模拟两个客户平台：签票据 → 由**平台后端**调中台兑换 → 把会话交给浏览器。

这不是产品代码，只用来在没有真实平台账号时走通并验收整条身份链路。

为什么必须"后端签、后端换"：如果浏览器能自己拿到票据或自己调兑换，那么任何人都
能拿别人的 `sub` 换一个会话——平台后端的登录态才是身份的唯一来源。这个脚本刻意
按这个拓扑实现：

```
浏览器 ──(cookie: mock 平台自己的登录态)──▶ 模拟平台后端
                                            │ 用平台私钥签 RS256 票据
                                            ├─▶ 中台 POST /api/auth/exchange
                                            ◀── Set-Cookie: agent_session=…
              ◀── 把中台 cookie 交回浏览器（开发环境：直接写 Set-Cookie）
```

启动：

```bash
python -m scripts.mock_identity_platform            # 默认 http://127.0.0.1:8090
python -m scripts.mock_identity_platform --port 8091
```

打开 <http://127.0.0.1:8090/>，可以在两个平台、多个用户之间切换。

密钥：首次运行会在 `secrets/mock-platform-*/` 下生成 RSA 密钥对（已入 .gitignore）。
中台只读公钥，私钥只有模拟平台持有。
"""
import argparse
import json
import os
import secrets
import sys as _sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.error
import urllib.parse

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

SECRETS = _ROOT / 'secrets'
ISSUER = 'https://mock-identity.local'
AUDIENCE = 'agent-hub-login'

# 两个平台、两套账号体系：sub 故意都叫 user-123，用来验证"不会串号"
PLATFORMS = {
    'a': {
        'connection_id': 'mock-platform-a',
        'platform_id': 'mock-a',
        'tenant_id': 'tenant-a',
        'identity_provider_id': 'mock-idp-a',   # 平台 A 自己的账号中心
        'users': [
            {'sub': 'user-123', 'name': '张三（平台A）'},
            {'sub': 'user-456', 'name': '李四（平台A）'},
        ],
    },
    'b': {
        'connection_id': 'mock-platform-b',
        'platform_id': 'mock-b',
        'tenant_id': 'tenant-b',
        'identity_provider_id': 'mock-idp-b',   # 平台 B 独立的账号体系
        'users': [
            {'sub': 'user-123', 'name': '王五（平台B）'},
        ],
    },
}


def _keypair(connection_id: str) -> tuple[str, str]:
    """取（或首次生成）某个平台的密钥对。私钥只留在 secrets/ 下。"""
    SECRETS.mkdir(exist_ok=True)
    private_path = SECRETS / f'{connection_id}.key.pem'
    public_path = SECRETS / f'{connection_id}.pub.pem'
    if not private_path.exists() or not public_path.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_path.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()))
        public_path.write_bytes(key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo))
        print(f'[mock] 生成密钥对：{private_path.name}')
    return (private_path.read_text(encoding='utf-8'),
            public_path.read_text(encoding='utf-8'))


def issue_ticket(platform_key: str, sub: str, *, ttl: int = 30,
                 now: int | None = None) -> str:
    """按约定签一张登录票据（RS256 + kid + 专用 typ）。"""
    platform = PLATFORMS[platform_key]
    private_pem, _ = _keypair(platform['connection_id'])
    stamp = int(time.time()) if now is None else int(now)
    claims = {
        'iss': ISSUER,
        'aud': AUDIENCE,
        'sub': sub,
        'tenant_id': platform['tenant_id'],
        'platform_id': platform['platform_id'],
        'iat': stamp,
        'exp': stamp + ttl,
        'jti': secrets.token_urlsafe(16),
    }
    return jwt.encode(claims, private_pem, algorithm='RS256',
                      headers={'kid': platform['connection_id'],
                               'typ': 'agent-login+jwt'})


def exchange_with_hub(hub_url: str, ticket: str) -> tuple[int, dict, str]:
    """**服务端**调中台兑换，返回 (状态码, 响应体, Set-Cookie 头)。"""
    body = json.dumps({'ticket': ticket}).encode('utf-8')
    request = urllib.request.Request(
        f'{hub_url.rstrip("/")}/api/auth/exchange', data=body, method='POST',
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode('utf-8') or '{}')
            return (response.status, payload,
                    response.headers.get('Set-Cookie') or '')
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', 'replace')
        try:
            payload = json.loads(detail)
        except ValueError:
            payload = {'detail': detail}
        return (exc.code, payload, '')


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>模拟客户平台</title>
<style>
 body{{font:15px/1.7 system-ui,-apple-system,"PingFang SC",sans-serif;
      max-width:760px;margin:40px auto;padding:0 20px;color:#1b2430}}
 h1{{font-size:20px}} .card{{border:1px solid #e2e6eb;border-radius:10px;
 padding:14px 16px;margin:12px 0}} a.btn{{display:inline-block;padding:6px 12px;
 border-radius:8px;background:#1b6ef3;color:#fff;text-decoration:none;margin:4px 6px 4px 0}}
 code{{background:#f2f4f7;padding:1px 5px;border-radius:4px}}
 .muted{{color:#6b7684;font-size:13px}} pre{{background:#f7f8fa;padding:10px;
 border-radius:8px;overflow:auto;font-size:12px}}
</style>
<h1>模拟客户平台（{platform_title}）</h1>
<p class="muted">平台自己的登录态 → 平台**后端**签票据 → 后端调中台兑换 →
浏览器拿到中台会话 cookie。切用户可验证"同一浏览器不同身份"与"数据隔离"。</p>

<div class="card"><b>当前平台登录态</b>
 <div class="muted">{session_line}</div></div>

<div class="card"><b>用这个平台的用户登录智能体</b><br>
 {user_links}
</div>

<div class="card"><b>切换到另一个模拟平台</b><br>
 {other_links}
</div>

<div class="card"><b>中台入口</b> <a class="btn" href="{hub_url}/" target="_blank">打开中台界面</a>
 <div class="muted">中台地址：<code>{hub_url}</code>（浏览器通过同源代理或直连访问）</div>
</div>

<div class="card"><b>最近一次兑换</b><pre>{last}</pre></div>
"""


def _html(platform_key: str, hub_url: str, sub: str, last: str) -> str:
    platform = PLATFORMS[platform_key]
    title = f'平台 {platform_key.upper()} · {platform["tenant_id"]}'
    user_links = ''.join(
        f'<a class="btn" href="/login?platform={platform_key}'
        f'&sub={urllib.parse.quote(user["sub"])}">{user["name"]}（{user["sub"]}）</a>'
        for user in platform['users'])
    other = ''.join(
        f'<a class="btn" href="/?platform={key}">平台 {key.upper()}</a>'
        for key in PLATFORMS if key != platform_key)
    session_line = (f'已登录：{sub}' if sub else '未登录（点下面的用户即可建立平台登录态）')
    return PAGE.format(platform_title=title, session_line=session_line,
                       user_links=user_links, other_links=other,
                       hub_url=hub_url, last=last or '（还没有兑换）')


class Handler(BaseHTTPRequestHandler):
    hub_url = 'http://127.0.0.1:8010'

    def log_message(self, fmt, *args):        # 安静一点
        pass

    def _send(self, status: int, body: str, headers=None):
        data = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        for key, value in (headers or []):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _session(self) -> tuple[str, str]:
        """模拟平台**自己的**登录态：cookie 里是 platform:sub。"""
        raw = ''
        for part in (self.headers.get('Cookie') or '').split(';'):
            name, _, value = part.strip().partition('=')
            if name == 'mock_login':
                raw = value
        platform, _, sub = raw.partition(':')
        return (platform if platform in PLATFORMS else 'a'), sub

    def do_GET(self):
        if not self.path.startswith('/login') and not self.path.startswith('/logout'):
            platform, sub = self._session()
            last = self.headers.get('X-Mock-Last', '')
            if self.path.startswith('/?'):
                query = self.path.split('?', 1)[1]
                for part in query.split('&'):
                    key, _, value = part.partition('=')
                    if key == 'platform' and value in PLATFORMS:
                        platform, sub = value, ''
            self._send(200, _html(platform, self.hub_url, sub, last))
            return

        if self.path.startswith('/logout'):
            self.send_response(302)
            self.send_header('Location', '/')
            self.send_header('Set-Cookie', 'mock_login=; Path=/; Max-Age=0')
            self.end_headers()
            return

        # /login?platform=a&sub=user-123 → 建立平台登录态 → 签票 → 后端调中台兑换
        query = dict(part.partition('=')[::2]
                     for part in self.path.partition('?')[2].split('&') if part)
        platform = query.get('platform', 'a')
        sub = urllib.parse.unquote(query.get('sub', ''))
        if platform not in PLATFORMS or not sub:
            self._send(400, '参数不对：需要 platform 与 sub')
            return

        ticket = issue_ticket(platform, sub)
        status, payload, set_cookie = exchange_with_hub(self.hub_url, ticket)
        last = json.dumps({'ticket_status': status, 'hub': payload},
                          ensure_ascii=False, indent=2)
        # 把中台会话 cookie 转给浏览器（开发环境同源直连；线上由平台代理转交）
        headers = [('Set-Cookie', f'mock_login={platform}:{sub}; Path=/; HttpOnly'),
                   ('Set-Cookie', set_cookie.replace('Secure; ', '')
                    .replace('; Secure', ''))] if set_cookie else \
                  [('Set-Cookie', f'mock_login={platform}:{sub}; Path=/; HttpOnly')]
        self._send(200, _html(platform, self.hub_url, sub, last), headers)


def main() -> int:
    parser = argparse.ArgumentParser(description='模拟两个客户平台（票据签发 + 兑换）')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8090)
    parser.add_argument('--hub', default=os.getenv('HUB_URL', 'http://127.0.0.1:8010'))
    args = parser.parse_args()

    Handler.hub_url = args.hub
    for key in PLATFORMS:
        _keypair(PLATFORMS[key]['connection_id'])
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'[mock] 模拟平台：http://{args.host}:{args.port}/  →  中台 {args.hub}')
    print('[mock] 中台需要登记这些连接（公钥在 secrets/ 下）：')
    for key, platform in PLATFORMS.items():
        _, public_pem = _keypair(platform['connection_id'])
        print(json.dumps({
            'connection_id': platform['connection_id'],
            'issuer': ISSUER,
            'audience': AUDIENCE,
            'algorithms': ['RS256'],
            'public_key': public_pem.splitlines()[0] + '…',
            'allowed_tenants': [platform['tenant_id']],
            'allowed_platforms': [platform['platform_id']],
            'identity_provider_id': platform['identity_provider_id'],
            'enabled': True,
        }, ensure_ascii=False))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n[mock] 退出')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
