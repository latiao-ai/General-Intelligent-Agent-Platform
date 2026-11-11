from webpage_agent.core import memory


def test_render_long_term_layers():
    text = memory.render_long_term(
        [{'predicate': '回复语言', 'value': '中文'}],
        [{'predicate': '项目', 'value': '网页研究智能体'}])
    assert '<memory>' in text
    assert '回复语言：中文' in text
    assert '当前工作区记忆' in text
    assert '项目：网页研究智能体' in text


def test_memory_cue():
    assert memory.has_memory_cue('记住我喜欢简洁回答')
    assert memory.has_memory_cue('以后都用中文')
    assert not memory.has_memory_cue('今天上海天气怎么样')


def test_render_session_memory():
    text = memory.render_session_memory('之前讨论了鼓浪屿行程。', {'目的地': '鼓浪屿'})
    assert '<session_memory>' in text
    assert '目的地：鼓浪屿' in text
    assert memory.render_session_memory('', {}) == ''
