from types import SimpleNamespace

from webpage_agent.agent.pi_runtime import _high_risk_tool, _is_confirmation


def tool(name: str, effect: str):
    return SimpleNamespace(name=name, effect=effect)


def test_low_risk_canvas_edits_are_not_gated():
    assert not _high_risk_tool(tool('mcp__beeftv__canvas_nodes_create', 'write'))
    assert not _high_risk_tool(tool('mcp__beeftv__canvas_node_update', 'write'))
    assert not _high_risk_tool(tool('mcp__beeftv__canvas_get', 'read'))


def test_destructive_and_paid_tools_are_gated():
    assert _high_risk_tool(tool('mcp__beeftv__canvas_node_delete', 'write'))
    assert _high_risk_tool(tool('mcp__beeftv__canvas_generation_submit', 'write'))
    assert _high_risk_tool(tool('mcp__beeftv__canvas_project_export', 'write'))
    assert _high_risk_tool(tool('mcp__beeftv__something', 'destructive'))
    assert _high_risk_tool(tool('mcp__beeftv__something', 'unknown'))


def test_confirmation_phrase():
    assert _is_confirmation('确认')
    assert _is_confirmation('同意执行')
    assert _is_confirmation('yes')
    assert not _is_confirmation('帮我创建一个图片节点')
