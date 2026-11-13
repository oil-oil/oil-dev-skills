#!/usr/bin/env python3
"""运行 oil-dev-skills 全部单元测试与子 Skill 结构检查（Python 3.10+ 标准库）。"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 10):
    sys.stderr.write("错误：需要 Python 3.10 或更高版本。\n")
    raise SystemExit(2)

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"
EXPECTED_SKILLS = (
    "oil-explore",
    "oil-plan",
    "oil-delegate",
    "oil-code-review",
    "oil-govern",
    "oil-bug-fix",
)
UNIVERSAL_SKILLS = {
    "oil-explore",
    "oil-plan",
    "oil-code-review",
    "oil-govern",
    "oil-bug-fix",
}


def _run_unittest_dir(test_dir: Path, label: str) -> bool:
    print(f"==> 运行测试：{label}")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(test_dir), "-p", "test_*.py", "-v"],
        cwd=str(REPO_ROOT),
        check=False,
    )
    return proc.returncode == 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行全仓回归测试与可选的 validate_skill.py 静态校验")
    parser.add_argument(
        "--validator",
        help="可选：传入 validate_skill.py 路径以附加运行 --public --strict --weak-model 校验",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ok = True

    root_tests = REPO_ROOT / "tests"
    if root_tests.is_dir() and not _run_unittest_dir(root_tests, "collection"):
        ok = False

    for name in EXPECTED_SKILLS:
        skill_dir = SKILLS_ROOT / name
        if not (skill_dir / "SKILL.md").is_file():
            sys.stderr.write(f"[FAIL] 缺少子 Skill：{skill_dir}\n")
            ok = False
            continue
        skill_tests = skill_dir / "tests"
        if skill_tests.is_dir() and not _run_unittest_dir(skill_tests, name):
            ok = False

        if args.validator:
            validator_path = Path(args.validator).expanduser().resolve()
            cmd = [
                sys.executable,
                str(validator_path),
                str(skill_dir),
                "--public",
                "--strict",
                "--weak-model",
            ]
            if name in UNIVERSAL_SKILLS:
                cmd.append("--universal")
            print(f"==> 校验规范：{name}")
            proc = subprocess.run(cmd, cwd=str(REPO_ROOT), check=False)
            if proc.returncode != 0:
                ok = False

    print("\nALL CHECKS PASSED" if ok else "\nCHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
