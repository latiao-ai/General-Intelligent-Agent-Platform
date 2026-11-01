import unittest
from unittest.mock import patch

import httpx
from starlette.requests import Request

from scripts import serve_frontend


class FrontendProxyTests(unittest.IsolatedAsyncioTestCase):
    async def check_proxy(self, path, target, status=200):
        seen = []

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'hello'

        def upstream(request):
            seen.append(request)
            return httpx.Response(status, stream=Body())

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        request = Request({'type': 'http', 'method': 'GET', 'path': '/',
                           'query_string': b'workspace_id=owned',
                           'headers': [(b'cookie', b'beeftv_session=existing'),
                                       (b'host', b'127.0.0.1:8001')]},
                          receive=self.receive)
        with patch.object(serve_frontend.httpx, 'AsyncClient', return_value=client):
            response = await serve_frontend.proxy(request, path)
            if hasattr(response, 'body_iterator'):
                async for _ in response.body_iterator:
                    pass
                await response.background()
        self.assertEqual(str(seen[0].url), target + '?workspace_id=owned')
        self.assertEqual(seen[0].headers['cookie'], 'beeftv_session=existing')
        self.assertEqual(response.status_code, status)
        return response

    async def receive(self):
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def test_page_uses_verified_platform_bridge(self):
        await self.check_proxy('', 'http://127.0.0.1:8004/api/hub/workbench/')

    async def test_conversation_uses_same_platform_bridge(self):
        await self.check_proxy('api/hub/workbench/api/sessions',
                               'http://127.0.0.1:8004/api/hub/workbench/api/sessions')

    async def test_health_still_reaches_backend(self):
        await self.check_proxy('healthz', 'http://127.0.0.1:8002/healthz')

    async def test_unauthenticated_page_does_not_bypass_login(self):
        response = await self.check_proxy('', 'http://127.0.0.1:8004/api/hub/workbench/', 401)
        self.assertIn('登录 BeeFTV', response.body.decode())
