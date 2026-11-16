from contextlib import contextmanager

from webpage_agent.core import memory
from webpage_agent.core.config import settings


@contextmanager
def _mode(value):
    """切抽取策略。settings 是 frozen dataclass，只能绕过 __setattr__ 改，用完还回去。"""
    before = settings.memory_extract_mode
    object.__setattr__(settings, 'memory_extract_mode', value)
    try:
        yield
    finally:
        object.__setattr__(settings, 'memory_extract_mode', before)


def test_should_extract_lets_model_decide():
    """默认 always：不靠关键词表，"我来自浙江台州"也要进抽取。"""
    assert settings.memory_extract_mode == 'always'
    assert memory.should_extract('我来自浙江台州')
    assert memory.should_extract('今天上海天气怎么样')      # 抽不抽由模型判断
    assert not memory.should_extract('   ')
    # 一句话寒暄不值得过一次模型（实测一次约 600 token）
    assert not memory.should_extract('好的')
    assert not memory.should_extract('谢谢')


def test_should_extract_cue_mode_still_catches_self_intro():
    with _mode('cue'):
        assert memory.should_extract('我来自浙江台州')
        assert memory.should_extract('我叫唐浩南，是一名 AI Agent 工程师')
        assert not memory.should_extract('今天上海天气怎么样')


def test_should_extract_off_mode():
    with _mode('off'):
        assert not memory.should_extract('记住我喜欢简洁回答')


def test_profile_key_accepts_place_words():
    """城市换一种说法（籍贯/老家/来自）也得能进画像，否则城市永远记不上。"""
    for key in ('城市', '籍贯', '老家', '家乡', '出生地', '现居', '来自'):
        assert memory.is_profile_key(key), key
    assert not memory.is_profile_key('本次议题')


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


def test_render_long_term_skips_profile_keys():
    """画像已有的键不再在 <memory> 里重复，避免同一偏好出现两个值。"""
    text = memory.render_long_term(
        [{'predicate': '称呼', 'value': '浩南'},
         {'predicate': '回复语言', 'value': '英文'}],
        [],
        override_keys={'称呼': '南哥'})
    assert '称呼：南哥' not in text
    assert '称呼：浩南' not in text
    assert '回复语言：英文' in text


def test_render_long_term_ignore_empty_override_keys():
    text = memory.render_long_term(
        [{'predicate': '称呼', 'value': '浩南'}], [], override_keys={})
    assert '称呼：浩南' in text
