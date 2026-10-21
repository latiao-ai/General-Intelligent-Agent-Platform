"""视频链路回归：失败先中止、MP4 真实媒体类型及授权下载。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from mcp.types import CallToolResult, TextContent

from app import outcome
from app.capabilities import video
from app.integrations import mcp_adapter


class LocalVideoTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_voice_stops_before_llm_or_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(video, 'server_root', return_value=(object(), Path(directory))), \
                    patch.object(video, 'structured_call', new_callable=AsyncMock) as model, \
                    patch.dict('os.environ', {'LOCAL_VIDEO_VOICE_MODEL': str(Path(directory)/'absent.onnx')}):
                with self.assertRaisesRegex(video.ToolError, '模型不完整'):
                    await video.make_local_explainer({'topic': '天空为什么是蓝色的'}, {'task_id': 't'})
                model.assert_not_awaited()

    async def test_business_failure_is_not_a_successful_video(self):
        response = CallToolResult(content=[TextContent(type='text', text='{"status":"error","code":"E_INVALID_INPUT","message":"missing model"}')])
        with self.assertRaises(mcp_adapter.MCPToolFailure):
            mcp_adapter.result_payload(response)
        self.assertEqual(outcome.final_status({'failure': 'missing model'}), 'failed')

    async def test_video_artifact_is_in_outcome_and_downloaded_as_video(self):
        import app.api as api
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory)/'final.mp4'
            file.write_bytes(b'video-container-fixture')
            artifact = {'kind': 'video', 'path': str(file), 'mime': 'video/mp4', 'size_bytes': file.stat().st_size}
            state = {'artifact': artifact}
            self.assertEqual(outcome.final_status(state), 'succeeded')
            self.assertEqual(outcome.build(state)['produced'][0]['kind'], 'video')
            record = SimpleNamespace(state=state, artifact_id=None)
            with patch.object(api, '_owned_task', AsyncMock(return_value=record)) as owned:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test') as client:
                    response = await client.get('/api/tasks/video-test/artifact')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers['content-type'], 'video/mp4')
                    self.assertIn('inline', response.headers['content-disposition'])
                    self.assertEqual(response.content, file.read_bytes())
                    owned.assert_awaited_once()
                    partial = await client.get('/api/tasks/video-test/artifact', headers={'Range': 'bytes=0-4'})
                    self.assertEqual(partial.status_code, 206)
                    self.assertEqual(partial.content, file.read_bytes()[:5])
