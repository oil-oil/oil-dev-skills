from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "review_helper.py"
SPEC = importlib.util.spec_from_file_location("review_helper", SCRIPT_PATH)
assert SPEC and SPEC.loader
review_helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review_helper)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(root: Path) -> None:
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "dev@example.com")
    _git(root, "config", "user.name", "Dev")
    _git(root, "config", "commit.gpgsign", "false")


class ReviewHelperTest(unittest.TestCase):
    def test_collects_working_tree_and_clean_branch_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)

            (root / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "feature.ts").write_text("export const a = 1;\n", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "init")

            # Feature branch with one committed change + one uncommitted edit
            _git(root, "checkout", "-b", "feat/demo")
            (root / "src" / "feature.ts").write_text("export const a = 2;\n", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "feat commit")

            # Clean working tree on feature branch auto-detects main...HEAD
            clean_report = review_helper.collect_review_context(root)
            self.assertEqual(clean_report["status"], "OK")
            self.assertEqual(clean_report["scope"], "auto:main...HEAD")
            self.assertTrue(clean_report["auto_fallback"])
            self.assertEqual(clean_report["summary"]["changed_files"], 1)

            # Uncommitted edit + --base main includes both committed and working-tree changes
            (root / "src" / "extra.ts").write_text("export const b = 3;\n", encoding="utf-8")
            base_report = review_helper.collect_review_context(root, base="main")
            self.assertEqual(base_report["scope"], "main..working-tree")
            self.assertFalse(base_report["auto_fallback"])
            paths = {f["path"] for f in base_report["files"]}
            self.assertEqual(paths, {"src/feature.ts", "src/extra.ts"})

    def test_non_ascii_deleted_renamed_and_spaced_paths_are_never_dropped(self) -> None:
        """回归：中文路径、删除、重命名曾被整体丢弃，并导致静默回退去审上一次提交。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            # 用户开启 noprefix 时 diff 头没有 a/ b/ 前缀，脚本必须不受影响
            _git(root, "config", "diff.noprefix", "true")
            (root / "a.py").write_text("a\n", encoding="utf-8")
            (root / "b.py").write_text("b\n", encoding="utf-8")
            (root / "说明.md").write_text("旧\n", encoding="utf-8")
            (root / "with space.py").write_text("x\n", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "one")
            (root / "c.py").write_text("c\n", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "two")

            with (root / "说明.md").open("a", encoding="utf-8") as fh:
                fh.write("新增一行\n")
            with (root / "with space.py").open("a", encoding="utf-8") as fh:
                fh.write("y\n")
            _git(root, "rm", "-q", "a.py")
            _git(root, "mv", "b.py", "renamed.py")
            (root / "新文件.py").write_text("x = 1\n", encoding="utf-8")

            report = review_helper.collect_review_context(root)
            self.assertEqual(report["scope"], "working-tree")
            self.assertFalse(report["auto_fallback"])
            by_path = {f["path"]: f for f in report["files"]}
            self.assertEqual(
                set(by_path),
                {"a.py", "renamed.py", "说明.md", "with space.py", "新文件.py"},
            )
            self.assertEqual(by_path["a.py"]["status"], "deleted")
            self.assertEqual(by_path["a.py"]["deleted"], 1)
            self.assertEqual(by_path["renamed.py"]["status"], "renamed")
            self.assertEqual(by_path["renamed.py"]["old_path"], "b.py")
            self.assertEqual(by_path["说明.md"]["hunks"], ["2-2"])
            self.assertEqual(by_path["with space.py"]["hunks"], ["2-2"])
            self.assertEqual(by_path["新文件.py"]["status"], "untracked")
            self.assertEqual(report["summary"]["deleted_files"], 1)

    def test_allow_patterns_flag_out_of_scope_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            (root / "src" / "export").mkdir(parents=True)
            (root / "src" / "export" / "job.ts").write_text("1\n", encoding="utf-8")
            (root / "src" / "other.ts").write_text("1\n", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "init")
            (root / "src" / "export" / "job.ts").write_text("2\n", encoding="utf-8")
            (root / "src" / "other.ts").write_text("2\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "job.test.ts").write_text("t\n", encoding="utf-8")

            report = review_helper.collect_review_context(root, allowed=["src/export/", "tests/*.test.ts"])
            self.assertEqual(report["out_of_scope_files"], ["src/other.ts"])

            no_allow = review_helper.collect_review_context(root)
            self.assertNotIn("out_of_scope_files", no_allow)

    def test_dev_profile_flags_high_risk_and_do_not_edit_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            (root / "AGENTS.md").write_text(
                "# Rules\n\n"
                "```markdown\n## 示例（oil-dev）\n- 高风险：`src/ui/**`\n```\n\n"
                "## 开发约定（oil-dev）\n\n"
                "- **快测**：`pytest tests/{target} -q`\n"
                "- 高风险：`src/storage/**`\n"
                "- 禁改: vendor/, generated/\n\n"
                "## Other\n- 高风险：`src/ui/**`\n",
                encoding="utf-8",
            )
            for rel in ("src/storage/db.py", "src/ui/view.py", "vendor/lib.py"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("1\n", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "init")
            for rel in ("src/storage/db.py", "src/ui/view.py", "vendor/lib.py"):
                (root / rel).write_text("2\n", encoding="utf-8")

            report = review_helper.collect_review_context(root)
            profile = report["dev_profile"]
            self.assertEqual(profile["source"], "AGENTS.md:8")
            self.assertEqual(profile["quick_test"], ["pytest tests/{target} -q"])
            # 代码块里的示例和后面其他小节里的同名条目都不能混进来
            self.assertEqual(profile["high_risk"], ["src/storage/**"])
            self.assertEqual(profile["do_not_edit"], ["vendor/", "generated/"])
            self.assertEqual(report["high_risk_files"], ["src/storage/db.py"])
            self.assertEqual(report["do_not_edit_files"], ["vendor/lib.py"])

            (root / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
            self.assertIsNone(review_helper.collect_review_context(root)["dev_profile"])

    def test_unquote_git_c_style_path(self) -> None:
        self.assertEqual(review_helper._unquote_git_path('"a\\"b\\346\\226\\260.py"'), 'a"b新.py')
        self.assertEqual(review_helper._unquote_git_path("b/plain path.py\t"), "b/plain path.py")


if __name__ == "__main__":
    unittest.main()
