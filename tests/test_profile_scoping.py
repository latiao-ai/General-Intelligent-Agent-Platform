"""画像按主体读取：显式 user 不依赖 contextvar。

聊天 turn 是独立协程，不能靠 `profile.use()` 绑主体（没有对应的 reset 时机）。
这里锁住"显式传 user 就读那个人的文件"这个契约，避免以后又退回全局状态。
"""
import pytest

from webpage_agent.core import profile
from webpage_agent.core.config import settings


@pytest.fixture()
def thin_profile(tmp_path):
    """把画像目录换到临时目录，避免碰到仓库里的 profile/USER.md。"""
    original = settings.profile_path
    object.__setattr__(settings, 'profile_path', tmp_path / 'USER.md')
    try:
        yield tmp_path
    finally:
        object.__setattr__(settings, 'profile_path', original)


def test_explicit_user_reads_own_file(thin_profile):
    # default 主体的画像
    profile.merge({'姓名': '张三'})
    assert profile.load()['姓名'] == '张三'
    # 另一个主体有独立文件，不继承 default 的内容
    assert profile.load(user='alice') == {}
    assert profile.with_profile('基础指令', user='alice') == '基础指令'


def test_default_user_falls_back_to_contextvar(thin_profile):
    profile.merge({'姓名': '张三'})
    token = profile.use('alice')
    try:
        assert profile.load() == {}
        assert profile.load(user='default')['姓名'] == '张三'
    finally:
        profile.reset(token)


def test_with_profile_marks_background_only(thin_profile):
    profile.merge({'偏好': '先给结论'})
    text = profile.with_profile('基础指令', user='default')
    assert '[用户画像]' in text
    assert '偏好：先给结论' in text
    assert '不要逐字复述' in text
