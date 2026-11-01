import os
import unittest
from unittest.mock import patch
from scripts import serve_api


class LanStartupTests(unittest.TestCase):
    def test_lan_listens_and_allows_browser_origin_without_disabling_auth(self):
        with patch.dict(os.environ, {'APP_ENV': 'development', 'AUTH_MODE': 'ticket'}, clear=True), \
             patch('dotenv.load_dotenv'), patch.object(serve_api, 'lan_address', return_value='192.168.1.20'), \
             patch.object(serve_api._sys, 'argv', ['serve_api', '--lan', '--port', '8010']), \
             patch.object(serve_api.uvicorn, 'run') as run:
            serve_api.main()
            self.assertEqual(run.call_args.kwargs['host'], '0.0.0.0')
            self.assertIn('http://192.168.1.20:8010', os.environ['AUTH_ALLOWED_ORIGINS'])
            self.assertEqual(os.environ['AUTH_COOKIE_SECURE'], 'false')
            self.assertEqual(os.environ['AUTH_MODE'], 'ticket')

    def test_default_stays_local(self):
        with patch.object(serve_api._sys, 'argv', ['serve_api']), patch.object(serve_api.uvicorn, 'run') as run:
            serve_api.main()
            self.assertEqual(run.call_args.kwargs['host'], '127.0.0.1')

    def test_production_rejects_lan(self):
        with patch.dict(os.environ, {'APP_ENV': 'production'}), \
             patch.object(serve_api._sys, 'argv', ['serve_api', '--lan']), \
             patch.object(serve_api.uvicorn, 'run') as run:
            with self.assertRaises(SystemExit):
                serve_api.main()
            run.assert_not_called()
