"""「我的画像」编辑框里该有什么。

文件是给机器和 git 看的（`# User Profile`、`> Version: n`），编辑框是给人看的。
以前两者共用一份原文，于是用户打开面板先看到一行 Markdown 标题、一行版本号，
时间还是 `2026-11-16T02:44:37.213932+00:00` 这种要做时区换算的串。

这里锁住编辑框的视图契约：只有「更新时间」+ 事实行，且往返保存不多出伪事实。
"""
from datetime import datetime

import pytest

from webpage_agent.core import profile
from webpage_agent.core.config import settings

FILE_TEXT = """# User Profile
> Last updated: 2026-11-16T02:44:37.213932+00:00
> Version: 5

姓名：唐浩南
职业：AI Agent 工程师
籍贯：浙江台州
"""


@pytest.fixture()
def profile_file(tmp_path):
    original = settings.profile_path
    path = tmp_path / 'USER.md'
    path.write_text(FILE_TEXT, encoding='utf-8')
    object.__setattr__(settings, 'profile_path', path)
    try:
        yield path
    finally:
        object.__setattr__(settings, 'profile_path', original)


def test_editable_drops_file_metadata(profile_file):
    text = profile.editable()
    assert '# User Profile' not in text
    assert 'Version' not in text
    assert 'Last updated' not in text


def test_editable_starts_with_readable_update_time(profile_file):
    expected = datetime.fromisoformat('2026-11-16T02:44:37.213932+00:00')
    expected = expected.astimezone().strftime('%Y-%m-%d %H:%M')
    text = profile.editable()
    assert text.startswith(f'更新时间：{expected}')
    # 微秒和 UTC 偏移不再出现在用户眼前
    assert '213932' not in text and '+00:00' not in text


def test_facts_keep_their_order(profile_file):
    lines = [line for line in profile.editable().splitlines() if line.strip()]
    assert lines[1:] == ['姓名：唐浩南', '职业：AI Agent 工程师', '籍贯：浙江台州']


def test_saving_the_form_adds_no_pseudo_fact(profile_file):
    """打开看一眼直接保存：那行「更新时间」不许变成档案里的一条事实。"""
    facts = profile.parse(profile.editable())
    assert facts == {'姓名': '唐浩南', '职业': 'AI Agent 工程师', '籍贯': '浙江台州'}
    assert profile.UPDATED_KEY not in facts


def test_round_trip_keeps_facts_unchanged(profile_file):
    before = profile.load()
    profile.save(profile.parse(profile.editable()))
    assert profile.load() == before


def test_empty_profile_has_nothing_to_edit(tmp_path):
    original = settings.profile_path
    object.__setattr__(settings, 'profile_path', tmp_path / 'USER.md')
    try:
        assert profile.editable() == ''
    finally:
        object.__setattr__(settings, 'profile_path', original)


if __name__ == '__main__':                                  # pragma: no cover
    pytest.main([__file__, '-q'])
