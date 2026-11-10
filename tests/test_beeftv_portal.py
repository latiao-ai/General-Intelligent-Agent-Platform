import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx

from webpage_agent.api import routes
from webpage_agent.governance import policies


class PortalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=routes.app),
                                        base_url='http://localhost')
        self.settings = patch.object(routes, 'settings', replace(routes.settings,
                              app_env='test', api_rate_limit_enabled=False))
        self.settings.start()
        self.env = patch.dict('os.environ', {'BEEFTV_LAUNCH_URL': '', 'BEEFTV_LAUNCH_ALLOWLIST': ''})
        self.env.start()
        routes.app.dependency_overrides[policies.current_principal] = lambda: policies.Principal(authenticated=True)

    async def asyncTearDown(self):
        routes.app.dependency_overrides.pop(policies.current_principal, None)
        self.env.stop()
        self.settings.stop()
        await self.client.aclose()

    async def test_default_disabled(self):
        self.assertEqual((await self.client.get('/api/beeftv/status')).json(), {'enabled': False})
        self.assertEqual((await self.client.get('/api/beeftv/launch')).status_code, 404)

    async def test_login_required_and_fixed_destination(self):
        url = 'http://127.0.0.1:8003/'
        with patch.dict('os.environ', {'BEEFTV_LAUNCH_URL': url, 'BEEFTV_LAUNCH_ALLOWLIST': url}):
            routes.app.dependency_overrides[policies.current_principal] = lambda: policies.Principal()
            self.assertEqual((await self.client.get('/api/beeftv/launch')).status_code, 401)
            routes.app.dependency_overrides[policies.current_principal] = lambda: policies.Principal(authenticated=True)
            response = await self.client.get('/api/beeftv/launch?next=https://evil.example')
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers['location'], url)
            self.assertEqual(response.headers['cache-control'], 'no-store')

    async def test_reject_unsafe_and_non_allowlisted_urls(self):
        for url in ('https://trusted.example@evil.example/', '//evil.example/',
                    'https://trusted.example/\\@evil.example/', 'https://trusted.example/?next=https://evil.example',
                    'https://trusted.example/#fragment', 'http://evil.example/', 'javascript:alert(1)',
                    'https://trusted.example/\r\nLocation: https://evil.example'):
            with self.subTest(url=url), patch.dict('os.environ', {
                    'BEEFTV_LAUNCH_URL': url, 'BEEFTV_LAUNCH_ALLOWLIST': url}):
                self.assertEqual((await self.client.get('/api/beeftv/status')).json(), {'enabled': False})
                self.assertEqual((await self.client.get('/api/beeftv/launch')).status_code, 404)
        with patch.dict('os.environ', {'BEEFTV_LAUNCH_URL': 'https://evil.example/',
                                      'BEEFTV_LAUNCH_ALLOWLIST': 'https://trusted.example/'}):
            self.assertEqual((await self.client.get('/api/beeftv/launch')).status_code, 404)

    async def test_production_rejects_loopback_http(self):
        url = 'http://localhost:8003/'
        with patch.object(routes, 'settings', replace(routes.settings, app_env='production')), \
                patch.dict('os.environ', {'BEEFTV_LAUNCH_URL': url, 'BEEFTV_LAUNCH_ALLOWLIST': url}):
            self.assertEqual((await self.client.get('/api/beeftv/launch')).status_code, 404)
