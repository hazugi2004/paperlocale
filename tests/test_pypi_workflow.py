"""PyPI 发布工作流的静态安全合同。"""

from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "publish-pypi.yml"


class PyPIPublishingWorkflowTest(unittest.TestCase):
    """防止后续维护意外绕过手动确认或扩大 OIDC 作业权限。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_publishing_is_manual_only(self) -> None:
        """创建标签或 Release 不得自动触发不可撤销的 PyPI 上传。"""

        trigger_block = self.workflow.split("\non:\n", 1)[1].split(
            "\npermissions:\n", 1
        )[0]
        self.assertIn("workflow_dispatch:", trigger_block)
        self.assertNotIn("push:", trigger_block)
        self.assertNotIn("release:", trigger_block)

    def test_oidc_permission_is_scoped_to_publish_job(self) -> None:
        """只有不运行仓库 shell 代码的发布作业可以申请短期 OIDC 令牌。"""

        self.assertEqual(self.workflow.count("id-token: write"), 1)
        publish_job = self.workflow.split("\n  publish-to-pypi:\n", 1)[1]
        self.assertIn("needs: prepare-distributions", publish_job)
        self.assertIn("name: pypi", publish_job)
        self.assertIn("id-token: write", publish_job)
        self.assertNotIn("run:", publish_job)

    def test_release_assets_and_actions_are_fixed(self) -> None:
        """上传对象必须来自已审计 Release，关键第三方 Action 必须固定到提交。"""

        self.assertIn("releases/download/${RELEASE_TAG}", self.workflow)
        self.assertIn("python -m twine check dist/*", self.workflow)
        self.assertIn(
            "pypa/gh-action-pypi-publish@"
            "dc37677b2e1c63e2034f94d8a5b11f265b73ba33",
            self.workflow,
        )
        self.assertNotIn("PYPI_API_TOKEN", self.workflow)
        self.assertNotIn("password:", self.workflow)
        self.assertNotIn("skip-existing:", self.workflow)


if __name__ == "__main__":
    unittest.main()
