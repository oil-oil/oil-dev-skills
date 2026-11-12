from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "repo_map.py"
SPEC = importlib.util.spec_from_file_location("repo_map", SCRIPT_PATH)
assert SPEC and SPEC.loader
repo_map = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repo_map)


class RepoMapTest(unittest.TestCase):
    def test_respects_gitignore_and_collects_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-b", "main"], cwd=str(root), check=True, capture_output=True)
            (root / ".gitignore").write_text(".build/\nnode_modules/\n", encoding="utf-8")
            (root / "package.json").write_text(
                json.dumps({"name": "demo", "scripts": {"test": "vitest run", "test:e2e": "playwright test"}}),
                encoding="utf-8",
            )
            (root / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
            (root / "scripts").mkdir()
            (root / "scripts" / "test.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            (root / "Sources" / "Core").mkdir(parents=True)
            (root / "Sources" / "Core" / "Engine.swift").write_text("public struct Engine {}\n", encoding="utf-8")
            # Ignored build artifacts must never pollute repo snapshot
            (root / ".build" / "debug").mkdir(parents=True)
            (root / ".build" / "debug" / "junk.o").write_text("binary", encoding="utf-8")

            report = repo_map.scan_repository(root)
            self.assertEqual(report["status"], "OK")
            dirs = {d["dir"] for d in report["directory_overview"]}
            self.assertIn("Sources/Core", dirs)
            self.assertTrue(all(".build" not in d for d in dirs))
            self.assertIn("scripts/test.py", report["defined_entries"]["scripts_dir"])
            self.assertTrue(any("test:e2e" in s for s in report["defined_entries"]["package_scripts"]))

    def test_surfaces_real_test_entrypoints_from_ci_justfile_and_long_package_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-b", "main"], cwd=str(root), check=True, capture_output=True)
            scripts = {f"tool{i}": "echo" for i in range(20)}
            scripts["test"] = "vitest run"
            (root / "package.json").write_text(json.dumps({"scripts": scripts}), encoding="utf-8")
            (root / "justfile").write_text("test:\n    cargo test\n", encoding="utf-8")
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / ".github" / "workflows" / "ci.yml").write_text(
                "jobs:\n  t:\n    steps:\n      - run: cargo test --workspace\n", encoding="utf-8"
            )

            entries = repo_map.scan_repository(root)["defined_entries"]
            # 原来只取前 15 个 script，排在后面的 test 会被截掉
            self.assertEqual(entries["package_scripts"][0], "test: vitest run")
            self.assertEqual(entries["just_recipes"], ["test"])
            self.assertEqual(entries["ci_run_steps"], ["ci.yml: cargo test --workspace"])

    def test_reads_dev_profile_from_claude_md_when_agents_md_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-b", "main"], cwd=str(root), check=True, capture_output=True)
            (root / "CLAUDE.md").write_text(
                "# Project\n\n### 开发约定 oil-dev\n- 快测命令：`swift test --filter {target}`\n- 门禁：`make ci`\n",
                encoding="utf-8",
            )
            profile = repo_map.scan_repository(root)["dev_profile"]
            self.assertEqual(profile["source"], "CLAUDE.md:3")
            self.assertEqual(profile["quick_test"], ["swift test --filter {target}"])
            self.assertEqual(profile["merge_gate"], ["make ci"])

    def test_missing_workspace_returns_error(self) -> None:
        report = repo_map.scan_repository(Path("/nonexistent/oil-explore-test-dir"))
        self.assertEqual(report["status"], "ERROR")


if __name__ == "__main__":
    unittest.main()
