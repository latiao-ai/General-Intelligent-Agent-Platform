import base64
import hashlib
import json
import time
import unittest
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.fernet import Fernet
from fastapi import FastAPI
from cryptography.hazmat.primitives import serialization

from webpage_agent.api import auth, oidc
from tests.test_ticket_auth import PRIVATE_A, PRIVATE_B


class Store:
    def __init__(self):
        self.rows = []
        self.used = set()

    async def redeem_ticket(self, **kwargs):
        if kwargs['jti'] in self.used:
            raise ValueError('replay')
        self.used.add(kwargs['jti'])
        self.rows.append(kwargs)
        return {'user_id': 'u1', 'auth_session_id': kwargs['session_id']}

    async def user(self, user_id):
        return {'status': 'active'}


class OidcTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store()
        app = FastAPI()
        app.include_router(oidc.router)
        app.state.store = self.store
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost')
        self.config = {'OIDC_ENABLED': 'true', 'OIDC_ISSUER': 'https://id.example/realm',
            'OIDC_CLIENT_ID': 'hub', 'OIDC_CLIENT_SECRET': 'secret',
            'OIDC_REDIRECT_URI': 'http://localhost/api/auth/oidc/callback',
            'OIDC_TRANSACTION_KEY': Fernet.generate_key().decode(),
            'OIDC_IDENTITY_MAP': json.dumps({'kc-user': {'tenant_id': 'tenant', 'platform_id': 'beeftv',
                'identity_provider_id': 'bee-accounts', 'subject': 'bee-user', 'connection_id': 'bee'}})}
        self.env = patch.dict('os.environ', self.config)
        self.env.start()
        self.setting = patch.object(oidc, 'settings', replace(oidc.settings, auth_mode='ticket',
                              auth_cookie_secure=False, app_env='test'))
        self.setting.start()
        self.metadata = patch.object(oidc, '_metadata', return_value={'authorization_endpoint': 'https://id.example/auth',
                       'token_endpoint': 'https://id.example/token', 'jwks_uri': 'https://id.example/keys'})
        self.metadata.start()

    async def asyncTearDown(self):
        self.metadata.stop()
        self.setting.stop()
        self.env.stop()
        await self.client.aclose()

    async def begin(self):
        response = await self.client.get('/api/auth/oidc/login')
        self.assertEqual(response.status_code, 302)
        self.assertIn('HttpOnly', response.headers['set-cookie'])
        self.assertIn('SameSite=lax', response.headers['set-cookie'])
        query = parse_qs(urlsplit(response.headers['location']).query)
        self.assertEqual(query['code_challenge_method'], ['S256'])
        transaction = json.loads(Fernet(self.config['OIDC_TRANSACTION_KEY'].encode()).decrypt(
            self.client.cookies.get(oidc._COOKIE).encode()))
        expected = base64.urlsafe_b64encode(hashlib.sha256(transaction['verifier'].encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(query['code_challenge'], [expected])
        return query

    async def finish(self, query, changes=None, state=None, bad_signature=False):
        private = serialization.load_pem_private_key(PRIVATE_A.encode(), password=None)
        signing_key = serialization.load_pem_private_key(PRIVATE_B.encode(), password=None) if bad_signature else private
        claims = {'iss': self.config['OIDC_ISSUER'], 'aud': 'hub', 'sub': 'kc-user',
                  'iat': int(time.time()), 'exp': int(time.time()) + 120, 'nonce': query['nonce'][0]}
        claims.update(changes or {})
        id_token = jwt.encode(claims, signing_key, algorithm='RS256', headers={'kid': 'test'})
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
        jwk['kid'] = 'test'

        class OAuth:
            def __init__(self, *args, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def fetch_token(self, endpoint, **kwargs):
                self_test.assertTrue(kwargs['code_verifier'])
                return {'id_token': id_token}

        class Keys:
            def __init__(self, *args, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def get(self, url):
                return httpx.Response(200, json={'keys': [jwk]}, request=httpx.Request('GET', url))

        self_test = self
        with patch.object(oidc, 'AsyncOAuth2Client', OAuth), patch.object(oidc.httpx, 'AsyncClient', Keys):
            return await self.client.get('/api/auth/oidc/callback', params={'code': 'code', 'state': state or query['state'][0]})

    async def test_login_and_session(self):
        query = await self.begin()
        response = await self.finish(query)
        self.assertEqual(response.headers['location'], '/')
        self.assertIn('agent_session=', response.headers.get_list('set-cookie')[0])
        self.assertIn('HttpOnly', response.headers.get_list('set-cookie')[0])
        self.assertEqual(self.store.rows[0]['subject'], 'bee-user')
        self.assertEqual(self.store.rows[0]['platform_id'], 'beeftv')
        self.assertNotIn(self.client.cookies.get('agent_session'), str(self.store.rows))
        self.assertEqual(self.store.rows[0]['session_digest'], auth.digest_of(self.client.cookies.get('agent_session')))

    async def test_invalid_state_nonce_issuer_audience_and_unmapped_subject(self):
        for changes, state in [({}, 'wrong'), ({'nonce': 'wrong'}, None),
                               ({'iss': 'https://evil'}, None), ({'aud': 'other'}, None),
                               ({'sub': 'unknown'}, None), ({'exp': 1}, None)]:
            query = await self.begin()
            response = await self.finish(query, changes, state)
            self.assertEqual(response.headers['location'], '/?login_error=oidc')
        query = await self.begin()
        response = await self.finish(query, bad_signature=True)
        self.assertEqual(response.headers['location'], '/?login_error=oidc')
        self.assertFalse(self.store.rows)

    async def test_missing_transaction_and_disabled(self):
        self.assertEqual((await self.client.get('/api/auth/oidc/callback?state=x&code=y')).headers['location'], '/?login_error=oidc')
        with patch.dict('os.environ', {'OIDC_ENABLED': 'false'}):
            self.assertEqual((await self.client.get('/api/auth/oidc/login')).status_code, 404)

    async def test_replay_rejected(self):
        query = await self.begin()
        cookie = self.client.cookies.get(oidc._COOKIE)
        await self.finish(query)
        self.client.cookies.set(oidc._COOKIE, cookie, domain='localhost.local', path=oidc._PATH)
        response = await self.finish(query)
        self.assertEqual(response.headers['location'], '/?login_error=oidc')
        self.assertEqual(len(self.store.rows), 1)
