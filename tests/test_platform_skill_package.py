"""平台技能包：管理员上传整包 → 用户安装时才解包。

这条链路以前是断的：控制台的「上传技能包」只在浏览器里读 `SKILL.md` 的正文
填进表单，`references/`、`scripts/` 直接丢掉；平台技能安装时只能按元数据**生成**
一份 SKILL.md，于是正文里写着"见 references/ops.md"、模型照着做却找不到文件。

这里钉住四件事：

1. **发布 ≠ 安装**：上传只把原件放进包仓库（`skillhub_packages_dir`），
   `skills_dir` 里不能因此多出目录——否则所有用户都会显示"已安装"；
2. **安装按包解**：用户点安装后 `references/`、`scripts/` 一起落地；
3. **散装文件也能传**：拖文件夹进来时浏览器给的是一堆 File，服务端压成 zip；
4. **包坏了不炸整条链**：包被删/没上传时退回"生成 SKILL.md"，安装仍然成功。
"""
import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from starlette.datastructures import UploadFile
from starlette.requests import Request

from webpage_agent.addons import skills as skills_lib
from webpage_agent.api import console_api, skillhub_api
from webpage_agent.core.config import PROJECT_ROOT, settings
from webpage_agent.governance.policies import Principal
from webpage_agent.skillhub import store as skillhub_store
from webpage_agent.store import platform_catalog

CATEGORIES = ('效率工具', '内容创作')

SKILL_MD = """---
name: Word 文档
description: 生成与检查 Word 文档
version: 1.2.0
---

# Word 文档

先把 references/operations.md 读一遍，再跑 scripts/render.py。
"""


def _admin():
    return Principal(authenticated=True, role='platform_admin',
                     user_id='u_admin', tenant_id='t1')


def _user(user='u_member'):
    return Principal(authenticated=True, role='member', user_id=user, tenant_id='t1')


def _request():
    return Request({'type': 'http', 'method': 'POST', 'path': '/api/console',
                    'headers': [], 'query_string': b'',
                    'scheme': 'http', 'server': ('test', 80)})


def _body(response):
    return json.loads(response.body)


def make_zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def _upload(skill_id: str, payload: bytes, filename='word.zip', *, paths=''):
    upload = UploadFile(filename=filename, file=io.BytesIO(payload))
    return asyncio.run(console_api.console_upload_platform_package(
        skill_id, _request(), files=[upload], paths=paths, principal=_admin()))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """每个用例一份技能库 + 包仓库 + 安装状态，不碰真实 profile/。"""
    # settings 是 frozen dataclass，只能绕过 __setattr__ 改；
    # 用例结束必须还原，否则后面的用例会写到这个临时目录里。
    saved = (settings.skills_dir, settings.skillhub_packages_dir)
    object.__setattr__(settings, 'skills_dir', tmp_path / 'skills')
    object.__setattr__(settings, 'skillhub_packages_dir', tmp_path / 'packages')
    monkeypatch.setattr(skillhub_store, '_PROFILE', tmp_path / 'skillhub-state')

    async def _empty(*args, **kwargs):
        return []

    monkeypatch.setattr(skillhub_api.remote, 'fetch_items', _empty)
    platform_catalog.upsert('skills', platform_catalog.validate_skill({
        'id': 'word', 'name': 'Word 文档', 'description': '生成与检查 Word 文档',
        'category': '效率工具', 'version': '1.2.0', 'prompt': '生成文档',
    }, CATEGORIES))
    yield tmp_path
    object.__setattr__(settings, 'skills_dir', saved[0])
    object.__setattr__(settings, 'skillhub_packages_dir', saved[1])


# ---------------------------------------------------------------- 上传


def test_upload_stores_package_but_does_not_install(env):
    """发布只落包，不落技能目录——落进 skills_dir 就等于全员已安装。"""
    info = _body(_upload('word', make_zip({
        'word/SKILL.md': SKILL_MD,
        'word/references/operations.md': '# 操作\n',
        'word/scripts/render.py': 'print(1)\n',
        'word/.DS_Store': 'junk',
    })))['package']

    assert info['files'] == 2
    assert info['file_list'] == ['references/operations.md', 'scripts/render.py']
    # SKILL.md 自己不算"附属文件"，但要能被解析出来
    assert info['name'] == 'Word 文档'
    assert (env / 'packages' / 'word' / '1.2.0.zip').is_file()
    assert not (env / 'skills' / 'word').exists()

    row = platform_catalog.load()['skills'][0]
    assert row['package_version'] == '1.2.0'
    assert row['package_files'] == 2


def test_install_unpacks_references_and_scripts(env):
    _upload('word', make_zip({
        'word/SKILL.md': SKILL_MD,
        'word/references/operations.md': '# 操作\n',
        'word/scripts/render.py': 'print(1)\n',
    }))
    result = asyncio.run(skillhub_api.install_skill('word', principal=_user()))

    assert result['package']['source'] == 'platform'
    installed = env / 'skills' / 'word'
    assert (installed / 'references' / 'operations.md').is_file()
    assert (installed / 'scripts' / 'render.py').is_file()
    assert (installed / 'SKILL.md').read_text(encoding='utf-8') == SKILL_MD


def test_loose_files_are_repacked_server_side(env):
    """拖文件夹进来：浏览器给一堆 File + 相对路径，服务端负责压成包。"""
    uploads = [
        UploadFile(filename='SKILL.md',
                   file=io.BytesIO(SKILL_MD.encode('utf-8'))),
        UploadFile(filename='ops.md', file=io.BytesIO('# 操作\n'.encode('utf-8'))),
    ]
    response = asyncio.run(console_api.console_upload_platform_package(
        'word', _request(), files=uploads,
        paths=json.dumps(['word/SKILL.md', 'word/references/ops.md']),
        principal=_admin()))
    assert _body(response)['package']['files'] == 1

    asyncio.run(skillhub_api.install_skill('word', principal=_user()))
    assert (env / 'skills' / 'word' / 'references' / 'ops.md').is_file()


def test_repack_rejects_traversal_path(env):
    """打包散装文件时不能信任前端给的相对路径。"""
    uploads = [
        UploadFile(filename='SKILL.md', file=io.BytesIO(SKILL_MD.encode('utf-8'))),
        UploadFile(filename='a.md', file=io.BytesIO(b'# A\n')),
    ]
    with pytest.raises(console_api.HTTPException) as exc:
        asyncio.run(console_api.console_upload_platform_package(
            'word', _request(), files=uploads,
            paths=json.dumps(['word/SKILL.md', '../outside.md']), principal=_admin()))
    assert exc.value.status_code == 400


def test_upload_requires_existing_skill(env):
    with pytest.raises(console_api.HTTPException) as exc:
        _upload('not-there', make_zip({'SKILL.md': SKILL_MD}))
    assert exc.value.status_code == 404


def test_package_without_skill_md_rejected(env):
    with pytest.raises(console_api.HTTPException) as exc:
        _upload('word', make_zip({'word/notes.md': '# 没有 SKILL.md\n'}))
    assert exc.value.status_code == 400
    assert 'SKILL.md' in exc.value.detail


def test_missing_description_falls_back_to_catalog(env):
    """SkillHub 生成的包常常没有 frontmatter：描述从目录条目补，别整包拒绝。"""
    _upload('word', make_zip({'SKILL.md': '# Word\n\n正文\n'}))
    asyncio.run(skillhub_api.install_skill('word', principal=_user()))
    text = (env / 'skills' / 'word' / 'SKILL.md').read_text(encoding='utf-8')
    assert 'description:' in text.split('---')[1]


def test_package_gone_falls_back_to_generated_skill_md(env):
    """包被删/换了机器：安装不能因此失败，只退化成"只有正文"。"""
    _upload('word', make_zip({
        'word/SKILL.md': SKILL_MD,
        'word/references/operations.md': '# 操作\n',
    }))
    for path in (env / 'packages' / 'word').iterdir():
        path.unlink()

    result = asyncio.run(skillhub_api.install_skill('word', principal=_user()))
    assert result['package'] is None
    installed = env / 'skills' / 'word'
    assert (installed / 'SKILL.md').is_file()
    assert not (installed / 'references').exists()


def test_remove_package_clears_catalog(env):
    _upload('word', make_zip({'word/SKILL.md': SKILL_MD,
                              'word/references/operations.md': '# 操作\n'}))
    result = _body(asyncio.run(console_api.console_delete_platform_package(
        'word', _request(), principal=_admin())))

    assert 'package' not in result
    assert not (env / 'packages' / 'word').exists()
    assert 'package' not in platform_catalog.load()['skills'][0]


def test_republish_keeps_package(env):
    """编辑技能（改概述/开关）不该把已经上传的包弄丢。"""
    _upload('word', make_zip({'word/SKILL.md': SKILL_MD,
                              'word/references/operations.md': '# 操作\n'}))
    platform_catalog.upsert('skills', platform_catalog.validate_skill({
        'id': 'word', 'name': 'Word 文档（改名）', 'description': '新简介',
        'category': '效率工具', 'version': '1.2.0', 'prompt': '新正文',
    }, CATEGORIES))
    row = platform_catalog.load()['skills'][0]
    assert row['name'] == 'Word 文档（改名）'
    assert row['package']


# ---------------------------------------------------------------- 详情页文件树


def test_file_tree_lists_nested_files(env):
    _upload('word', make_zip({
        'word/SKILL.md': SKILL_MD,
        'word/references/operations.md': '# 操作\n',
        'word/scripts/render.py': 'print(1)\n',
    }))
    # 还没安装：磁盘上不应该有目录
    assert skills_lib.get_skill('word') is None
    asyncio.run(skillhub_api.install_skill('word', principal=_user()))

    installed = skills_lib.get_skill('word')
    assert installed['file_tree'] == ['SKILL.md', 'references/operations.md',
                                      'scripts/render.py']
    # 老的 files 字段保持一层，别把既有前端打断
    assert 'references' in installed['files']


def test_skill_detail_exposes_file_tree(env):
    asyncio.run(skillhub_api.install_skill('word', principal=_user()))
    item = asyncio.run(skillhub_api.get_skill('word', principal=_user()))
    assert item['file_tree'] == ['SKILL.md']


def test_multipart_endpoint_accepts_browser_style_upload(env):
    """FastAPI 的参数解析也得通：File 列表 + Form 的 paths 就是前端真实发的形状。

    直接调函数会绕过 multipart 解析，所以这里单独用 TestClient 打一次 HTTP——
    "函数能跑、接口 422"这种错只有走到这一层才会露出来。
    """
    client = _client()

    payload = make_zip({
        'word/SKILL.md': SKILL_MD,
        'word/references/operations.md': '# 操作\n',
    })
    res = client.post('/api/console/platform/skills/word/package',
                      files=[('files', ('word.zip', payload, 'application/zip'))])
    assert res.status_code == 200, res.text
    assert res.json()['package']['files'] == 1


def test_multipart_endpoint_repacks_loose_files(env):
    client = _client()
    res = client.post(
        '/api/console/platform/skills/word/package',
        files=[
            ('files', ('SKILL.md', SKILL_MD.encode('utf-8'), 'text/markdown')),
            ('files', ('ops.md', '# 操作\n'.encode('utf-8'), 'text/markdown')),
        ],
        data={'paths': json.dumps(['word/SKILL.md', 'word/references/ops.md'])})
    assert res.status_code == 200, res.text
    assert res.json()['package']['file_list'] == ['references/ops.md']


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from webpage_agent.governance import policies

    app = FastAPI()
    app.include_router(console_api.router)
    app.dependency_overrides[policies.current_principal] = _admin
    return TestClient(app)


# ---------------------------------------------------------------- 部署可搬运


def test_package_path_is_stored_relative(env):
    """目录里存相对包仓库的路径：换机器/换目录后这份 JSON 才不会全指向空。"""
    _upload('word', make_zip({'word/SKILL.md': SKILL_MD}))
    raw = platform_catalog.load()['skills'][0]['package']
    assert raw == 'word/1.2.0.zip'
    assert not Path(raw).is_absolute()
    assert platform_catalog.package_path(raw) == env / 'packages' / 'word' / '1.2.0.zip'


def test_package_survives_moving_the_packages_dir(env):
    """整包仓库搬到别的目录（本地 → 服务器）之后，安装仍能按包解。"""
    _upload('word', make_zip({
        'word/SKILL.md': SKILL_MD,
        'word/references/operations.md': '# 操作\n',
    }))
    moved = env / 'moved-packages'
    moved.mkdir()
    shutil.move(str(env / 'packages' / 'word'), str(moved / 'word'))
    object.__setattr__(settings, 'skillhub_packages_dir', moved)

    result = asyncio.run(skillhub_api.install_skill('word', principal=_user()))
    assert result['package']['source'] == 'platform'
    assert (env / 'skills' / 'word' / 'references' / 'operations.md').is_file()


def test_legacy_absolute_package_path_still_resolves(env, tmp_path):
    """老数据存的是绝对路径，不能因为换了存储格式就集体失效。"""
    absolute = tmp_path / 'legacy' / 'word' / '1.2.0.zip'
    absolute.parent.mkdir(parents=True)
    absolute.write_bytes(make_zip({'word/SKILL.md': SKILL_MD}))
    platform_catalog.set_skill_package('word', package=str(absolute),
                                       version='1.2.0', files=0,
                                       size=absolute.stat().st_size)

    assert platform_catalog.package_path(
        platform_catalog.load()['skills'][0]['package']) == absolute


def test_upload_keeps_original_filename(env):
    """磁盘上按 `<版本>.zip` 存，但回显要给上传时的原名——否则认不出是谁传的。"""
    _upload('word', make_zip({'word/SKILL.md': SKILL_MD}), filename='我的word包.zip')
    body = _body(asyncio.run(console_api.console_platform_package(
        'word', principal=_admin())))
    assert body['package']['original_name'] == '我的word包.zip'
    assert body['package']['filename'] == '1.2.0.zip'

    # 散装文件（拖文件夹）是服务端压的包，没有"原名"可言，别编一个 skill.zip 出来。
    asyncio.run(console_api.console_upload_platform_package(
        'word', _request(),
        files=[UploadFile(filename='SKILL.md', file=io.BytesIO(SKILL_MD.encode('utf-8'))),
               UploadFile(filename='ops.md', file=io.BytesIO(b'# \xe6\x93\x8d\xe4\xbd\x9c\n'))],
        paths=json.dumps(['word/SKILL.md', 'word/references/ops.md']),
        principal=_admin()))
    body = _body(asyncio.run(console_api.console_platform_package(
        'word', principal=_admin())))
    assert body['package']['original_name'] == ''


def test_state_dir_env_is_honoured(tmp_path):
    """`SKILLHUB_STATE_DIR` 要真的生效，否则部署时安装清单只能留在代码目录里。

    这里用子进程而不是 reload：`settings` 是导入时就建好的单例，进程内改环境
    变量不会重建它，reload 又会把别的模块手里的引用换掉。
    """
    env = {**os.environ,
           'SKILLHUB_STATE_DIR': str(tmp_path / 'state'),
           'PYTHONPATH': str(PROJECT_ROOT / 'src')}
    out = subprocess.run(
        [sys.executable, '-c',
         'from webpage_agent.skillhub import store; print(store._PROFILE)'],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT), timeout=90)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == str(tmp_path / 'state')


# ---------------------------------------------------------------- 查看包内容


def test_package_listing_lists_skill_files(env):
    _upload('word', make_zip({
        'word/SKILL.md': SKILL_MD,
        'word/references/operations.md': '# 操作\n',
        'word/logo.png': '\x00binary',
        '__MACOSX/word/._SKILL.md': 'junk',
    }))
    body = _body(asyncio.run(console_api.console_platform_package(
        'word', principal=_admin())))
    info = body['package']
    assert info['filename'] == '1.2.0.zip'
    assert [e['path'] for e in info['entries']] == [
        'SKILL.md', 'logo.png', 'references/operations.md']
    assert [e['binary'] for e in info['entries']] == [False, True, False]


def test_package_file_content_is_readable(env):
    _upload('word', make_zip({
        'word/SKILL.md': SKILL_MD,
        'word/references/operations.md': '# 操作\n第一步\n',
    }))
    body = _body(asyncio.run(console_api.console_platform_package_file(
        'word', path='references/operations.md', principal=_admin())))
    assert body['file']['text'] == '# 操作\n第一步\n'
    assert body['file']['binary'] is False


def test_package_file_rejects_traversal(env):
    _upload('word', make_zip({'word/SKILL.md': SKILL_MD}))
    with pytest.raises(console_api.HTTPException) as exc:
        asyncio.run(console_api.console_platform_package_file(
            'word', path='../outside.md', principal=_admin()))
    assert exc.value.status_code == 400


def test_package_listing_reports_missing_file(env):
    """目录里记着包、文件却没了：要说"缺失"，不能说"本来就没有"。"""
    _upload('word', make_zip({'word/SKILL.md': SKILL_MD}))
    for path in (env / 'packages' / 'word').iterdir():
        path.unlink()
    body = _body(asyncio.run(console_api.console_platform_package(
        'word', principal=_admin())))
    assert body['package'] is None
    assert body['missing'] is True


# ---------------------------------------------------------------- 前端契约


def test_admin_page_uploads_package_as_multipart():
    """控制台的「上传技能包」必须真的把文件传上去，而不是只填表单。"""
    source = (PROJECT_ROOT / 'src' / 'webpage_agent' / 'api' / 'web'
              / 'admin.html').read_text(encoding='utf-8')
    assert '/package' in source
    assert 'new FormData()' in source
    assert "form.append('files'" in source


def test_admin_form_has_no_version_field():
    """版本号不再让人填、也不在列表里显示——它只会成为永不过期的 1.0.0。"""
    source = (PROJECT_ROOT / 'src' / 'webpage_agent' / 'api' / 'web'
              / 'admin.html').read_text(encoding='utf-8')
    assert 'sfVersion' not in source
    assert '<th>版本</th>' not in source


def test_admin_package_card_can_open_file_contents():
    """技能包卡片要能展开文件树、点开单个文件——只显示"已存 5 个文件"没法核对。"""
    source = (PROJECT_ROOT / 'src' / 'webpage_agent' / 'api' / 'web'
              / 'admin.html').read_text(encoding='utf-8')
    assert 'sfPackageTree' in source
    assert "'/file?path='" in source
    assert 'sfPackageToggle' in source


# ---------------------------------------------------------------- 换包同步
#
# 「技能包内容 · 1 个文件」这个线上现象，根因不是前后端不同步，而是两边读的
# 根本不是一份东西：管理端列的是包内清单，用户端列的是已装目录里的文件；而
# 安装是幂等的（见目录就返回），管理端后来补传的 references/、scripts/ 从来没
# 有落地。下面钉住三件事：上传即同步、列表能自愈、手动入口能补。

FULL_PACKAGE = {
    'word/SKILL.md': SKILL_MD,
    'word/references/operations.md': '# 操作\n',
    'word/scripts/render.py': 'print(1)\n',
}
FULL_TREE = ['SKILL.md', 'references/operations.md', 'scripts/render.py']


def _install_without_package(env):
    """复现线上那种"先装、后传包"：装的时候没有包，只生成了一份 SKILL.md。"""
    asyncio.run(skillhub_api.install_skill('word', principal=_user()))
    assert skills_lib.get_skill('word')['file_tree'] == ['SKILL.md']


def test_upload_syncs_installed_copy_without_user_action(env):
    """管理端传包后立刻覆盖已装目录：用户不用重装，也不用点任何按钮。"""
    _install_without_package(env)
    data = _body(_upload('word', make_zip(FULL_PACKAGE)))

    assert data['sync']['files'] == 3
    installed = env / 'skills' / 'word'
    assert (installed / 'references' / 'operations.md').is_file()
    assert (installed / 'scripts' / 'render.py').is_file()
    assert skills_lib.get_skill('word')['file_tree'] == FULL_TREE


def test_upload_records_package_digest(env):
    """包指纹要落到目录里：版本号常年停在 1.0.0，只有指纹能说明"包换了"。"""
    _install_without_package(env)
    _upload('word', make_zip(FULL_PACKAGE))

    row = platform_catalog.load()['skills'][0]
    assert row['package_digest']
    assert skills_lib.get_skill('word')['package_digest'] == row['package_digest']


def test_upload_does_not_install_for_everyone(env):
    """没人装过的技能，上传不能凭空变出目录——那等于绕过安装。"""
    data = _body(_upload('word', make_zip(FULL_PACKAGE)))
    assert data['sync'] is None
    assert not (env / 'skills' / 'word').exists()


def test_reinstall_refreshes_stale_install(env):
    """"点安装也不更新"的老毛病：装过了但内容旧，就按包覆盖，而不是直接返回。"""
    _install_without_package(env)
    # 造一个"上传时同步没成功"的状态。直接改模块属性而不是 monkeypatch：
    # `monkeypatch.undo()` 会把 env fixture 注册的补丁一起撤掉，技能库就跑回真的 profile 了。
    original = skills_lib.sync_installed
    skills_lib.sync_installed = lambda *a, **k: None
    try:
        _upload('word', make_zip(FULL_PACKAGE))
    finally:
        skills_lib.sync_installed = original
    assert skills_lib.get_skill('word')['file_tree'] == ['SKILL.md']

    asyncio.run(skillhub_api.install_skill('word', principal=_user()))
    assert skills_lib.get_skill('word')['file_tree'] == FULL_TREE


def test_listing_self_heals_stale_install(env):
    """用户下次打开技能中心就自己对齐：不需要谁去点按钮或删目录重装。"""
    _install_without_package(env)

    def broken(*args, **kwargs):
        raise OSError('磁盘不可写')

    original = skills_lib.sync_installed
    skills_lib.sync_installed = broken
    try:
        _upload('word', make_zip(FULL_PACKAGE))
    finally:
        skills_lib.sync_installed = original
    assert skills_lib.get_skill('word')['file_tree'] == ['SKILL.md']

    payload = asyncio.run(skillhub_api.list_skills(
        kind='skill', q='', category='', sort='comprehensive',
        tab='recommend', installed=False, existing=False, principal=_user()))
    item = next(x for x in payload['items'] if x['id'] == 'word')
    assert item['file_tree'] == FULL_TREE


def test_resync_endpoint_fixes_drifted_dir(env):
    """目录被改坏（同步失败、手删文件）时，管理端的「重新同步」能补齐。"""
    _upload('word', make_zip(FULL_PACKAGE))
    asyncio.run(skillhub_api.install_skill('word', principal=_user()))
    (env / 'skills' / 'word' / 'references' / 'operations.md').unlink()

    data = _body(asyncio.run(console_api.console_sync_platform_package(
        'word', _request(), principal=_admin())))
    assert data['installed'] is True
    assert data['synced']['files'] == 3
    assert (env / 'skills' / 'word' / 'references' / 'operations.md').is_file()


def test_resync_endpoint_skips_when_nobody_installed(env):
    """没装过就没有可同步的目录：接口要如实说"没装"，而不是悄悄装一份。"""
    _upload('word', make_zip(FULL_PACKAGE))
    data = _body(asyncio.run(console_api.console_sync_platform_package(
        'word', _request(), principal=_admin())))
    assert data['installed'] is False
    assert data['synced'] is None
    assert not (env / 'skills' / 'word').exists()


def test_sync_drops_files_removed_from_package(env):
    """换包后旧附件要清掉：留着会被模型当成"还有这个脚本"照着读。"""
    _upload('word', make_zip({'word/SKILL.md': SKILL_MD,
                              'word/scripts/old.py': 'print(1)\n'}))
    asyncio.run(skillhub_api.install_skill('word', principal=_user()))
    assert (env / 'skills' / 'word' / 'scripts' / 'old.py').is_file()

    _upload('word', make_zip({'word/SKILL.md': SKILL_MD,
                              'word/references/operations.md': '# 操作\n'}))
    assert not (env / 'skills' / 'word' / 'scripts' / 'old.py').exists()
    assert (env / 'skills' / 'word' / 'references' / 'operations.md').is_file()


def test_sync_keeps_enabled_state(env):
    """同步只动文件，不动启停：用户手动关掉的技能不该因为管理端换包被打开。"""
    _upload('word', make_zip(FULL_PACKAGE))
    asyncio.run(skillhub_api.install_skill('word', principal=_user()))
    skills_lib.set_enabled('word', False)
    assert skills_lib.get_skill('word')['enabled'] is False

    asyncio.run(console_api.console_sync_platform_package(
        'word', _request(), principal=_admin()))
    assert skills_lib.get_skill('word')['enabled'] is False


def test_console_lists_sync_state(env):
    """管理端要能看见"用户手上还是旧的"，而不是等用户来问。"""
    _install_without_package(env)
    row = _client().get('/api/console/platform/skills').json()['items'][0]
    assert row['installed'] is True
    assert row['sync_state'] == 'stale'


def test_admin_package_card_can_resync():
    """管理端要有手动兜底入口：自动同步失败时不用去删用户目录。"""
    source = (PROJECT_ROOT / 'src' / 'webpage_agent' / 'api' / 'web'
              / 'admin.html').read_text(encoding='utf-8')
    assert 'sfPackageSync' in source
    assert "'/sync'" in source


def test_user_detail_page_hides_package_content():
    """用户端不再列包内文件：那是发布方核对的东西，不是用户要读的信息。"""
    source = (PROJECT_ROOT / 'src' / 'webpage_agent' / 'api' / 'web'
              / 'index.html').read_text(encoding='utf-8')
    assert '技能包内容 ·' not in source
    assert 'buildFilesBlock' not in source
