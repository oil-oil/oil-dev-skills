#!/usr/bin/env python3
"""检查或同步 skills/oil-delegate 与独立 oil-delegate 仓库目录（不使用 Git submodule，保证 npx skills add 开箱即用）。"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

if sys.version_info < (3, 10):
    sys.stderr.write("错误：需要 Python 3.10 或更高版本。\n")
    raise SystemExit(2)

REPO_ROOT = Path(__file__).resolve().parents[1]
EMBEDDED_DIR = REPO_ROOT / "skills" / "oil-delegate"
DEFAULT_STANDALONE = REPO_ROOT.parent / "oil-delegate"
IGNORE_NAMES = {".git", ".gitignore", "__pycache__", ".DS_Store", ".pytest_cache"}


def _collect_manifest(base: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(base.rglob("*")):
        rel_parts = path.relative_to(base).parts
        if any(part in IGNORE_NAMES or part.endswith(".pyc") for part in rel_parts):
            continue
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest[path.relative_to(base).as_posix()] = digest
    return manifest


def _copy_tree(src: Path, dst: Path) -> int:
    dst.mkdir(parents=True, exist_ok=True)
    src_manifest = _collect_manifest(src)
    dst_manifest = _collect_manifest(dst)

    for stale_rel in set(dst_manifest) - set(src_manifest):
        stale_path = dst / stale_rel
        if stale_path.is_file():
            stale_path.unlink()

    copied = 0
    for rel, digest in src_manifest.items():
        if dst_manifest.get(rel) == digest:
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, target)
        copied += 1
    return copied


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检查或同步 skills/oil-delegate 与独立仓库")
    parser.add_argument("--standalone", default=str(DEFAULT_STANDALONE), help="独立 oil-delegate 目录路径")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="仅检查两边文件哈希是否一致（默认）")
    group.add_argument("--pull", action="store_true", help="从独立 oil-delegate 同步到本仓库 skills/oil-delegate")
    group.add_argument("--push", action="store_true", help="从本仓库 skills/oil-delegate 同步回独立 oil-delegate")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    standalone = Path(args.standalone).expanduser().resolve()

    if not standalone.is_dir():
        sys.stderr.write(f"提示：未找到独立 oil-delegate 目录：{standalone}\n")
        return 1

    if args.pull:
        changed = _copy_tree(standalone, EMBEDDED_DIR)
        print(f"已从独立仓库同步到 skills/oil-delegate（更新 {changed} 个文件）。")
        return 0

    if args.push:
        changed = _copy_tree(EMBEDDED_DIR, standalone)
        print(f"已从 skills/oil-delegate 同步回独立仓库（更新 {changed} 个文件）。")
        return 0

    left = _collect_manifest(EMBEDDED_DIR)
    right = _collect_manifest(standalone)
    if left == right:
        print(f"OK：skills/oil-delegate 与 {standalone} 完全一致（共 {len(left)} 个文件）。")
        return 0

    only_embedded = sorted(set(left) - set(right))
    only_standalone = sorted(set(right) - set(left))
    differing = sorted(k for k in set(left) & set(right) if left[k] != right[k])
    if only_embedded:
        print("仅存在于 skills/oil-delegate：", ", ".join(only_embedded))
    if only_standalone:
        print("仅存在于独立仓库：", ", ".join(only_standalone))
    if differing:
        print("内容不一致：", ", ".join(differing))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
