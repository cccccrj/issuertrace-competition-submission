"""The deploy archive, checked for the property that makes it an archive.

The package exists so a bare server needs nothing but the tarball.  It had one
hole: `web/dist` was left out and the generated README told the operator to run
`npm install && npm run build` on the target, which quietly made Node and a
reachable npm registry part of the deployment contract for an artefact whose
whole point is running with no network.  These read the script rather than run
it — building an archive needs the live database — so they check the two things
a reader of the script can be wrong about: that the bundle is copied in, and
that no instruction anywhere still tells the target to build it.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts" / "package_deploy.sh").read_text(encoding="utf-8")


class ArchiveContentsTests(unittest.TestCase):
    def test_the_client_bundle_travels_in_the_archive(self):
        self.assertIn('cp -a web/dist "$PAYLOAD/web/dist"', SCRIPT)

    def test_a_missing_bundle_fails_the_build_rather_than_the_server(self):
        # Without it api/main.py mounts nothing and every page is a 404; that is
        # worth failing on the machine that can still fix it.
        self.assertIn("web/dist/index.html", SCRIPT)
        self.assertRegex(SCRIPT, r"web/dist/index\.html[^\n]*\|\|")

    def test_the_bundle_is_listed_in_what_the_package_contains(self):
        contents = SCRIPT.split("部署包内容")[1].split("包里没有")[0]
        self.assertIn("web/dist/", contents)

    def test_the_gpu_runbook_and_imported_neighbours_travel_too(self):
        # The neighbours are keyed by paragraph id, so they only make sense next
        # to the database snapshot taken in the same run.
        self.assertIn('cp docs/submission/08-GPU向量计算测试报告.md "$PAYLOAD/docs/submission/"', SCRIPT)
        self.assertIn('cp -a data/state/dense "$PAYLOAD/data/state/dense"', SCRIPT)
        contents = SCRIPT.split("部署包内容")[1].split("包里没有")[0]
        self.assertIn("data/state/dense/", contents)


class TargetInstructionsTests(unittest.TestCase):
    def test_nothing_tells_the_target_to_run_npm(self):
        # The archive is unpacked on a machine with no network. An npm step
        # anywhere in the instructions is a broken deployment, not a slow one.
        readme = SCRIPT.split("cat > \"$PAYLOAD/README.txt\"")[1]
        self.assertNotIn("npm install", readme)
        self.assertNotIn("npm run build", readme)

    def test_the_rebuild_checklist_covers_only_the_secrets(self):
        # 账号在 2026-09-06 从 config/accounts.json 搬进了库里的 platform_users 表，
        # 这份清单（和这条断言）当时没跟着改。现在要重建的只剩 .env。
        checklist = SCRIPT.split("包里没有、必须在服务器上重建的")[1].split("落地步骤")[0]
        self.assertIn(".env", checklist)
        self.assertIn("AUTH_SECRET", checklist)
        self.assertNotIn("config/accounts.json", checklist)
        self.assertNotIn("web/dist", checklist)

    def test_the_checklist_says_the_database_carries_the_accounts(self):
        # 把库拷到别的环境等于把账号一起拷过去——这件事必须写在拆包人看得见的地方。
        checklist = SCRIPT.split("包里没有、必须在服务器上重建的")[1].split("落地步骤")[0]
        self.assertIn("platform_users", checklist)
        self.assertIn("disable", checklist)

    def test_the_landing_steps_migrate_the_schema_before_the_checks(self):
        # 平台只读打开数据库，自己建不了新列；漏了这一步的症状是某页 500 而不是启动失败。
        steps = SCRIPT.split("落地步骤")[1]
        self.assertIn("processor.repair_labels", steps)
        self.assertLess(steps.index("processor.repair_labels"), steps.index("processor.verify"))

    def test_the_closing_hint_does_not_send_the_operator_to_build(self):
        closing = SCRIPT.split("下一步：")[1]
        self.assertNotIn("npm", closing)


if __name__ == "__main__":
    unittest.main()
