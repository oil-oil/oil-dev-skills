from __future__ import annotations

import importlib.util
import inspect
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"
EXPECTED_SKILLS = {
    "oil-explore",
    "oil-plan",
    "oil-delegate",
    "oil-code-review",
    "oil-govern",
    "oil-bug-fix",
}
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


class CollectionStructureTest(unittest.TestCase):
    def test_expected_five_skills_exist_without_release_bloat(self) -> None:
        actual = {
            p.name
            for p in SKILLS_DIR.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        }
        self.assertEqual(actual, EXPECTED_SKILLS)
        for name in EXPECTED_SKILLS:
            skill_md = SKILLS_DIR / name / "SKILL.md"
            self.assertTrue(skill_md.is_file(), f"Missing {skill_md}")
            text = skill_md.read_text(encoding="utf-8")
            self.assertIn(f"name: {name}", text)

    def test_root_readme_mentions_all_skills_and_has_valid_local_links(self) -> None:
        readme = REPO_ROOT / "README.md"
        self.assertTrue(readme.is_file())
        content = readme.read_text(encoding="utf-8")
        for name in EXPECTED_SKILLS:
            self.assertIn(name, content)
        for match in MD_LINK_RE.finditer(content):
            target = match.group(1).strip()
            if target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            clean = target.split("#", 1)[0]
            resolved = (REPO_ROOT / clean).resolve()
            self.assertTrue(resolved.exists(), f"Broken link in README.md: {target}")

    def test_shared_entrypoint_discovery_stays_identical(self) -> None:
        """各 Skill 独立安装所以各自带一份副本；这里防止改了一边忘了另一边。"""
        sources = {}
        for name, rel in (
            ("repo_map", "oil-explore/scripts/repo_map.py"),
            ("audit_repo", "oil-govern/scripts/audit_repo.py"),
        ):
            spec = importlib.util.spec_from_file_location(f"shared_{name}", SKILLS_DIR / rel)
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            sources[name] = module
        left, right = sources["repo_map"], sources["audit_repo"]
        for func in ("_prioritized_package_scripts", "_collect_just_recipes", "_collect_ci_run_steps"):
            self.assertEqual(
                inspect.getsource(getattr(left, func)),
                inspect.getsource(getattr(right, func)),
                f"{func} differs between repo_map.py and audit_repo.py",
            )
        for const in ("PRIORITY_SCRIPT_HINTS", "CI_RUN_KEY_RE", "JUST_RECIPE_RE"):
            self.assertEqual(getattr(left, const), getattr(right, const), const)

    def test_shared_dev_profile_parser_stays_identical(self) -> None:
        modules = {}
        for name, rel in (
            ("repo_map", "oil-explore/scripts/repo_map.py"),
            ("review_helper", "oil-code-review/scripts/review_helper.py"),
            ("audit_repo", "oil-govern/scripts/audit_repo.py"),
        ):
            spec = importlib.util.spec_from_file_location(f"profile_{name}", SKILLS_DIR / rel)
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            modules[name] = module
        reference = modules["audit_repo"]
        for name, module in modules.items():
            self.assertEqual(
                inspect.getsource(module._load_dev_profile),
                inspect.getsource(reference._load_dev_profile),
                f"_load_dev_profile differs in {name}",
            )
            for const in (
                "DEV_PROFILE_FILES",
                "DEV_PROFILE_KEYS",
                "DEV_PROFILE_LABELS",
                "DEV_PROFILE_GLOB_KEYS",
                "DEV_PROFILE_HEADING_RE",
                "DEV_PROFILE_ITEM_RE",
                "DEV_PROFILE_CODE_RE",
            ):
                self.assertEqual(getattr(module, const), getattr(reference, const), f"{const} differs in {name}")
        self.assertEqual(
            inspect.getsource(modules["review_helper"]._matches_any),
            inspect.getsource(reference._matches_any),
        )

    def test_shared_preferences_script_stays_identical(self) -> None:
        """oil-explore 与 oil-plan 读写同一份个人偏好文件，脚本必须逐字一致。"""
        copies = [SKILLS_DIR / name / "scripts" / "preferences.py" for name in ("oil-explore", "oil-plan")]
        self.assertEqual(copies[0].read_bytes(), copies[1].read_bytes(), "preferences.py differs between oil-explore and oil-plan")

    def test_cli_entrypoints_respond_to_help(self) -> None:
        scripts = [
            REPO_ROOT / "scripts" / "verify_repo.py",
            REPO_ROOT / "scripts" / "sync_delegate.py",
            SKILLS_DIR / "oil-explore" / "scripts" / "repo_map.py",
            SKILLS_DIR / "oil-explore" / "scripts" / "preferences.py",
            SKILLS_DIR / "oil-plan" / "scripts" / "preferences.py",
            SKILLS_DIR / "oil-delegate" / "scripts" / "delegate.py",
            SKILLS_DIR / "oil-code-review" / "scripts" / "review_helper.py",
            SKILLS_DIR / "oil-govern" / "scripts" / "audit_repo.py",
        ]
        for script in scripts:
            proc = subprocess.run(
                [sys.executable, str(script), "--help"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, f"{script} --help failed: {proc.stderr}")


if __name__ == "__main__":
    unittest.main()
