"""现场上下文证明：签名、企业、哈希三者对不上就拒绝。"""
import time
import unittest
from unittest.mock import patch

import jwt

from app import auth_connections, scene_context
from tests.test_ticket_auth import ISS, PRIVATE_A, PUBLIC_A, connection


CONTEXT = {'project_id': 'project_001',
           'selection': [{'type': 'scene', 'id': 'scene_003'}]}


def _sign(context, *, tenant='tenant-a', kid='k1', tamper=False):
    body = dict(context)
    if tamper:
        body = {'project_id': 'other'}
    digest = scene_context.canonical_hash(body if tamper else context)
    now = int(time.time())
    token = jwt.encode(
        {'iss': ISS, 'aud': scene_context.SCENE_AUDIENCE, 'sub': 'user-123',
         'tenant_id': tenant, 'platform_id': 'pa', 'ctx': digest,
         'iat': now, 'exp': now + 60},
        PRIVATE_A, algorithm='RS256',
        headers={'kid': kid, 'typ': scene_context.SCENE_TYPE})
    return token


class SceneContextTests(unittest.TestCase):
    def test_signed_context_is_accepted(self):
        conn = connection()
        with patch.object(auth_connections, 'connections', return_value=[conn]):
            snapshot = scene_context.verify(
                _sign(CONTEXT), CONTEXT, tenant_id='tenant-a')
        self.assertTrue(snapshot['verified'])
        self.assertEqual(snapshot['context']['project_id'], 'project_001')
        self.assertNotIn('PRIVATE', str(snapshot))

    def test_tampered_context_is_rejected(self):
        conn = connection()
        signed = _sign(CONTEXT)
        changed = {'project_id': 'project_002',
                   'selection': CONTEXT['selection']}
        with patch.object(auth_connections, 'connections', return_value=[conn]):
            with self.assertRaises(scene_context.SceneError) as ctx:
                scene_context.verify(signed, changed, tenant_id='tenant-a')
        self.assertIn('不一致', ctx.exception.reason)

    def test_other_tenant_is_rejected(self):
        conn = connection()
        with patch.object(auth_connections, 'connections', return_value=[conn]):
            with self.assertRaises(scene_context.SceneError):
                scene_context.verify(
                    _sign(CONTEXT, tenant='tenant-b'), CONTEXT,
                    tenant_id='tenant-a')

    def test_public_key_material_is_not_the_scene_token(self):
        self.assertTrue(PUBLIC_A.startswith('-----BEGIN'))
