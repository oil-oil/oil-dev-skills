from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "audit_repo.py"
SPEC = importlib.util.spec_from_file_location("audit_repo", SCRIPT_PATH)
assert SPEC and SPEC.loader
audit_repo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_repo)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "dev@example.com")
    _git(root, "config", "user.name", "Dev")
    _git(root, "config", "commit.gpgsign", "false")


def _commit_file(cwd: Path, name: str, content: str, message: str) -> None:
    (cwd / name).write_text(content, encoding="utf-8")
    _git(cwd, "add", name)
    _git(cwd, "commit", "-m", message)


class AuditRepoTest(unittest.TestCase):
    def test_collects_objective_links_churn_large_files_and_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)

            (root / "package.json").write_text(
                json.dumps({"name": "demo", "scripts": {"test": "vitest run", "test:e2e": "playwright test"}}),
                encoding="utf-8",
            )
            (root / "Sources").mkdir()
            # URL-encoded '+' in filename should resolve cleanly without false broken-link
            (root / "Sources" / "Controller+Disk.swift").write_text("struct S {}\n", encoding="utf-8")
            big_code = "\n".join(f"let x{i} = {i}" for i in range(420)) + "\n"
            (root / "Sources" / "BigDocument.swift").write_text(big_code, encoding="utf-8")

            (root / "AGENTS.md").write_text(
                "# Rules\n- See [valid](Sources/Controller%2BDisk.swift) and [missing](docs/nope.md).\n",
                encoding="utf-8",
            )
            (root / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "init")

            report = audit_repo.run_governance_audit(root, scope="all")
            self.assertEqual(report["status"], "OK")

            broken_targets = [b["target"] for b in report["docs"]["broken_links_root"]]
            self.assertEqual(broken_targets, ["docs/nope.md"])

            claude_entry = next(r for r in report["docs"]["root_markdown_files"] if r["path"] == "CLAUDE.md")
            self.assertTrue(claude_entry["uses_at_agents_ref"])

            large_paths = [f["path"] for f in report["code_and_tests"]["large_files_over_400_lines"]]
            self.assertIn("Sources/BigDocument.swift", large_paths)

    def test_merge_state_distinguishes_squash_rebase_noff_fresh_and_unmerged(self) -> None:
        """回归：squash 合并曾检测不到，刚建的空分支曾被误判为“已合入”。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repo"
            root.mkdir()
            _init_repo(root)
            _commit_file(root, "a.py", "1\n", "init")

            def worktree(branch: str) -> Path:
                path = base / f"wt-{branch}"
                _git(root, "worktree", "add", "-q", "-b", branch, str(path))
                return path

            sq = worktree("sq")
            _commit_file(sq, "s.py", "1\n", "s1")
            _commit_file(sq, "s.py", "1\n2\n", "s2")
            _commit_file(sq, "t.py", "t\n", "s3")
            rb = worktree("rb")
            _commit_file(rb, "r.py", "r\n", "r1")
            noff = worktree("noff")
            _commit_file(noff, "n.py", "n\n", "n1")
            wip = worktree("wip")
            _commit_file(wip, "w.py", "w\n", "w1")

            _commit_file(root, "a.py", "1\nmain moves\n", "main moves")
            _git(root, "merge", "--squash", "sq")
            _git(root, "commit", "-m", "squash sq")
            _git(root, "cherry-pick", "rb")
            _git(root, "merge", "--no-ff", "noff", "-m", "merge noff")

            fresh = worktree("fresh")
            dirty = worktree("dirty")
            _commit_file(dirty, "d.py", "d\n", "d1")
            _git(root, "merge", "--no-ff", "dirty", "-m", "merge dirty")
            (dirty / "local.txt").write_text("not committed\n", encoding="utf-8")
            self.assertTrue(fresh.is_dir())

            info = audit_repo.run_governance_audit(root, scope="worktrees")["worktrees"]
            by_branch = {wt["branch"]: wt for wt in info["worktrees"]}

            self.assertEqual(by_branch["main"]["merge_state"], "default_branch")
            self.assertFalse(by_branch["main"]["cleanup_candidate"])
            self.assertEqual(by_branch["sq"]["merge_state"], "squash_merged")
            self.assertEqual(by_branch["rb"]["merge_state"], "rebase_merged")
            self.assertEqual(by_branch["noff"]["merge_state"], "merged")
            for branch in ("sq", "rb", "noff"):
                self.assertTrue(by_branch[branch]["merged_into_default"], branch)
                self.assertTrue(by_branch[branch]["cleanup_candidate"], branch)

            self.assertEqual(by_branch["fresh"]["merge_state"], "no_own_commits")
            self.assertFalse(by_branch["fresh"]["merged_into_default"])
            self.assertFalse(by_branch["fresh"]["cleanup_candidate"])

            self.assertEqual(by_branch["wip"]["merge_state"], "unmerged")
            self.assertEqual(by_branch["wip"]["ahead"], 1)

            self.assertEqual(by_branch["dirty"]["merge_state"], "merged")
            self.assertEqual(by_branch["dirty"]["uncommitted_entries"], 1)
            self.assertFalse(by_branch["dirty"]["cleanup_candidate"])

            merged_names = {name.split("(")[0] for name in info["merged_local_branches"]}
            self.assertEqual(merged_names, {"sq", "rb", "noff", "dirty"})

    def test_doc_age_is_reported_beyond_churn_window_and_for_non_ascii_names(self) -> None:
        """回归：超过 80 次提交未修改的文档曾完全不显示新鲜度，中文文件名曾查不到。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            (root / "docs").mkdir()
            (root / "README.md").write_text("# Demo\n", encoding="utf-8")
            (root / "docs" / "设计说明.md").write_text("# 设计\n", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "docs")
            for i in range(audit_repo.CHURN_COMMIT_WINDOW + 5):
                _git(root, "commit", "--allow-empty", "-m", f"c{i}")

            docs = audit_repo.run_governance_audit(root, scope="docs")["docs"]
            readme = next(r for r in docs["root_markdown_files"] if r["path"] == "README.md")
            self.assertEqual(readme["commits_since_touch"], audit_repo.CHURN_COMMIT_WINDOW + 5)
            self.assertEqual(readme["days_since_touch"], 0)
            design = next(d for d in docs["docs_sample"] if d["path"] == "docs/设计说明.md")
            self.assertEqual(design["commits_since_touch"], audit_repo.CHURN_COMMIT_WINDOW + 5)

    def test_dev_profile_reports_missing_keys_and_stale_high_risk_globs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            (root / "src" / "storage").mkdir(parents=True)
            (root / "src" / "storage" / "db.py").write_text("1\n", encoding="utf-8")
            (root / "AGENTS.md").write_text(
                "# Rules\n\n## Dev profile (oil-dev)\n\n"
                "- quick-test: `pytest -q {target}`\n"
                "- high-risk: `src/storage/**`, `src/legacy/**`\n"
                "- do-not-edit: `generated/`\n",
                encoding="utf-8",
            )
            _git(root, "add", ".")
            _git(root, "commit", "-m", "init")

            docs = audit_repo.run_governance_audit(root, scope="docs")["docs"]
            self.assertEqual(docs["dev_profile"]["quick_test"], ["pytest -q {target}"])
            self.assertEqual(docs["dev_profile_missing_keys"], ["merge_gate"])
            self.assertEqual(docs["dev_profile_unmatched_patterns"], ["src/legacy/**"])

            (root / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
            docs = audit_repo.run_governance_audit(root, scope="docs")["docs"]
            self.assertIsNone(docs["dev_profile"])

    def test_discovers_ci_steps_just_recipes_and_keeps_test_scripts_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            scripts = {f"tool{i}": "echo" for i in range(25)}
            scripts["test:unit"] = "vitest run"
            (root / "package.json").write_text(json.dumps({"scripts": scripts}), encoding="utf-8")
            (root / "justfile").write_text(
                'set shell := ["bash", "-c"]\nalias t := test\ntest filter="":\n    pytest {{filter}}\n@lint:\n    ruff check\n',
                encoding="utf-8",
            )
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / ".github" / "workflows" / "ci.yml").write_text(
                "jobs:\n  t:\n    steps:\n      - uses: actions/checkout@v4\n      - run: npm ci\n"
                "      - name: Test\n        run: |\n          npm test -- --run\n          # comment\n\n"
                "          npm run e2e\n      - run: 'make lint'\n",
                encoding="utf-8",
            )

            ct = audit_repo.run_governance_audit(root, scope="tests")["code_and_tests"]
            defined = ct["defined_scripts"]
            self.assertEqual(defined["package_json"][0], "test:unit: vitest run")
            self.assertEqual(len(defined["package_json"]), 20)
            self.assertEqual(defined["justfile"], ["test", "lint"])
            self.assertEqual(
                defined["ci_run_steps"],
                ["ci.yml: npm ci", "ci.yml: npm test -- --run", "ci.yml: npm run e2e", "ci.yml: make lint"],
            )
            self.assertTrue(ct["has_ci_config"])

    def test_board_groups_branches_and_flags_main_drift(self) -> None:
        """分支看板：只在本机的分支、主干本地多出的提交、跟踪错对象、别处推上来的分支都要标出来。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            origin = base / "origin.git"
            _git(base, "init", "-q", "--bare", "-b", "main", str(origin))
            root = base / "repo"
            _git(base, "clone", "-q", str(origin), str(root))
            for key, value in (("user.email", "dev@example.com"), ("user.name", "Dev"), ("commit.gpgsign", "false")):
                _git(root, "config", key, value)
            _commit_file(root, "a.py", "1\n", "init")
            _git(root, "push", "-q", "-u", "origin", "main")
            _git(root, "remote", "set-head", "origin", "main")

            _git(root, "switch", "-q", "-c", "feat/done")
            _commit_file(root, "done.py", "d\n", "done")
            _git(root, "push", "-q", "-u", "origin", "feat/done")
            _git(root, "switch", "-q", "main")
            _git(root, "merge", "-q", "--no-ff", "feat/done", "-m", "Merge pull request #1")
            _git(root, "push", "-q", "origin", "main")

            _git(root, "switch", "-q", "-c", "feat/local-only")
            _commit_file(root, "local.py", "x\ny\n", "local work")
            _git(root, "switch", "-q", "main")

            _git(root, "switch", "-q", "-c", "fix/wrong-upstream", "--track", "origin/main")
            _commit_file(root, "w.py", "w\n", "wrong upstream")
            _git(root, "switch", "-q", "main")

            _git(root, "switch", "-q", "-c", "feat/active")
            _commit_file(root, "active.py", "a\n", "active work")
            _git(root, "push", "-q", "-u", "origin", "feat/active")
            _git(root, "switch", "-q", "main")
            wt = base / "wt-active"
            _git(root, "worktree", "add", "-q", str(wt), "feat/active")
            (wt / "active.py").write_text("a\nmore\n", encoding="utf-8")

            other = base / "other"
            _git(base, "clone", "-q", str(origin), str(other))
            for key, value in (("user.email", "dev@example.com"), ("user.name", "Dev"), ("commit.gpgsign", "false")):
                _git(other, "config", key, value)
            _git(other, "switch", "-q", "-c", "feat/elsewhere")
            _commit_file(other, "elsewhere.py", "e\n", "pushed from another machine")
            _git(other, "push", "-q", "-u", "origin", "feat/elsewhere")

            _commit_file(root, "direct.py", "oops\n", "committed straight to main")

            board = audit_repo.run_governance_audit(root, scope="board", fetch=True)["board"]
            self.assertEqual(board["base"], "origin/main")
            self.assertTrue(board["fetch"]["ok"])
            self.assertEqual(board["pr_source"], "unavailable")
            self.assertIn("main_has_local_commits", board["repo_flags"])
            self.assertEqual([c["subject"] for c in board["main"]["local_only_commits"]], ["committed straight to main"])

            by_name = {b["name"]: b for b in board["branches"] + board["remote_only_branches"]}
            self.assertEqual(by_name["feat/done"]["group"], "cleanable")
            self.assertNotIn("changes", by_name["feat/done"])

            local_only = by_name["feat/local-only"]
            self.assertEqual(local_only["group"], "needs_decision")
            self.assertIn("local_only", local_only["flags"])
            self.assertEqual(local_only["changes"]["commits"][0]["subject"], "local work")
            self.assertEqual(local_only["changes"]["files"], [{"path": "local.py", "added": 2, "deleted": 0}])

            self.assertIn("tracks_other_branch", by_name["fix/wrong-upstream"]["flags"])
            self.assertEqual(by_name["fix/wrong-upstream"]["group"], "needs_decision")

            active = by_name["feat/active"]
            self.assertEqual(active["group"], "in_progress")
            self.assertIn("uncommitted_changes", active["flags"])
            self.assertEqual(Path(active["worktree"]["path"]).resolve(), wt.resolve())
            self.assertEqual(active["worktree"]["uncommitted_sample"], ["active.py"])

            elsewhere = by_name["feat/elsewhere"]
            self.assertEqual(elsewhere["kind"], "remote_only")
            self.assertEqual(elsewhere["group"], "in_progress")
            self.assertEqual(elsewhere["changes"]["commits"][0]["subject"], "pushed from another machine")

            self.assertEqual(sorted(board["groups"]["needs_decision"]), ["feat/local-only", "fix/wrong-upstream"])
            self.assertGreaterEqual(board["remote_merged_branches_total"], 1)

    def test_board_marks_idle_branches_stale_and_dirty_main_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            _commit_file(root, "a.py", "1\n", "init")
            _git(root, "switch", "-q", "-c", "feat/old")
            (root / "old.py").write_text("o\n", encoding="utf-8")
            _git(root, "add", "old.py")
            subprocess.run(
                ["git", "commit", "-q", "-m", "old work"], cwd=str(root), check=True, capture_output=True,
                env={**os.environ, "GIT_AUTHOR_DATE": "2020-01-01T00:00:00", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00"},
            )
            _git(root, "switch", "-q", "main")
            (root / "a.py").write_text("changed\n", encoding="utf-8")

            board = audit_repo.run_governance_audit(root, scope="board")["board"]
            self.assertEqual(board["base"], "main")
            self.assertIn("main_worktree_dirty", board["repo_flags"])
            old = board["branches"][0]
            self.assertEqual(old["name"], "feat/old")
            self.assertIn("stale", old["flags"])
            self.assertIn("local_only", old["flags"])
            self.assertEqual(old["group"], "needs_decision")
            self.assertEqual(board["main_worktree"]["uncommitted_sample"], ["a.py"])


if __name__ == "__main__":
    unittest.main()
