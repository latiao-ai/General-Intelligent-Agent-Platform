"""前端选择器的源码级约束（单文件前端，没有构建步骤，也不起浏览器）。

这些断言防的是"改一个功能把另一个功能悄悄关掉"：

- 输入框里的 `+ → 专家/技能` 必须打开**会话级选择器**，不能退回技能中心整页；
- 选择必须随消息以 `addon_selection` 发出（新会话没有会话行，先 PATCH 会 404）；
- 技能中心整页的入口仍存在（`WB_OPEN_HUB`），否则"去技能中心安装"点了没反应；
- 三个选择键在脚本里必须按 `null`（恢复预设）/ `[]`（明确不要）区分发送。
"""
import unittest
from pathlib import Path

PAGE = Path('src/webpage_agent/api/web/index.html')


@unittest.skipUnless(PAGE.exists(), '前端文件不在预期路径')
class AddonsFrontendSourceTests(unittest.TestCase):
    def setUp(self):
        self.source = PAGE.read_text(encoding='utf-8')

    def test_plus_menu_opens_session_pickers(self):
        self.assertIn("window.WB_ADDONS_OPEN_EXPERT", self.source)
        self.assertIn("window.WB_ADDONS_OPEN_SKILL", self.source)
        # 加号菜单的两个入口都走选择器，而不是直接跳整页
        self.assertIn("if (window.WB_ADDONS_OPEN_EXPERT) window.WB_ADDONS_OPEN_EXPERT();",
                      self.source)
        self.assertIn("if (window.WB_ADDONS_OPEN_SKILL) window.WB_ADDONS_OPEN_SKILL();",
                      self.source)

    def test_session_message_carries_addon_selection(self):
        self.assertIn("addon_selection: addonPatch", self.source)
        self.assertIn("window.WB_ADDONS_PENDING", self.source)

    def test_addon_chips_and_modals_exist(self):
        for marker in ('id="addonChips"', 'id="expertPickerModal"',
                       'id="skillPickerModal"', 'id="spPreset"'):
            self.assertIn(marker, self.source)
        self.assertIn('PATCH', self.source)
        self.assertIn("/addons", self.source)

    def test_skill_center_entry_is_exported(self):
        self.assertIn('window.WB_OPEN_HUB = openHub;', self.source)

    def test_restore_preset_sends_null_not_empty(self):
        # 「恢复专家预设」= 删掉显式键（null）；「取消全部技能」才是 []
        self.assertIn("patchAddons({skill_ids: null}", self.source)
        self.assertIn("patchAddons({expert_id: null}", self.source)


if __name__ == '__main__':                                  # pragma: no cover
    unittest.main()
