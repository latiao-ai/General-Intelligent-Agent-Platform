from webpage_agent.agent.pi_runtime import _extract_tool_urls, _tool_make_visible


def test_extract_urls_nested():
    content = {'project': {'canvas_url': 'https://www.liblib.tv/canvas?projectId=1'},
               'nodes': [{'image': 'https://cdn.example/a.png'}],
               'token': 'https://secret.example/token'}
    urls = _extract_tool_urls(content)
    assert 'https://www.liblib.tv/canvas?projectId=1' in urls
    assert 'https://cdn.example/a.png' in urls
    assert all('secret.example' not in u for u in urls)


def test_make_visible():
    assert _tool_make_visible({'makeVisible': True})
    assert _tool_make_visible({'result': {'make_visible': True}})
    assert not _tool_make_visible({'makeVisible': False})
