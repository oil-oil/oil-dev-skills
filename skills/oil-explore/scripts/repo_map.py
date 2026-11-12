#!/usr/bin/env python3
"""采集代码库目录结构、文件分布、脚本入口与工作树快照（仅使用 Python 3.10+ 标准库）。"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

if sys.version_info < (3, 10):
    sys.stderr.write("错误：需要 Python 3.10 或更高版本。\n")
    raise SystemExit(2)

CONTRACT_VERSION = 1

FALLBACK_SKIP_DIRS = {"node_modules", "vendor", "dist", "build", "target", "out", "__pycache__", "coverage"}

ROOT_DOC_CANDIDATES = ("AGENTS.md", "DESIGN.md", "REVIEW.md", "README.md", "CONTRIBUTING.md")
MANIFEST_CANDIDATES = (
    "package.json",
    "pnpm-workspace.yaml",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "Package.swift",
    "Makefile",
    "justfile",
)


# ---- 测试与构建入口发现（与 oil-govern/scripts/audit_repo.py 保持逐字一致，由仓库级测试校验） ----
PRIORITY_SCRIPT_HINTS = ("test", "spec", "e2e", "lint", "check", "type", "build", "verify", "ci")
CI_RUN_KEY_RE = re.compile(r"^(\s*)(?:-\s+)?run:\s*(.*)$")
JUST_RECIPE_RE = re.compile(r"^@?([A-Za-z_][A-Za-z0-9_-]*)(?:\s+[^:\n]*)?:(?!=)", re.MULTILINE)


def _prioritized_package_scripts(scripts: dict[str, Any], limit: int) -> list[str]:
    """测试、检查、构建类脚本排在前面，避免被截断。"""
    items = [(k, v) for k, v in scripts.items() if isinstance(k, str)]
    first = [kv for kv in items if any(hint in kv[0].lower() for hint in PRIORITY_SCRIPT_HINTS)]
    rest = [kv for kv in items if kv not in first]
    return [f"{k}: {v}" for k, v in (first + rest)[:limit]]


def _collect_just_recipes(root: Path, limit: int) -> list[str]:
    for name in ("justfile", "Justfile", ".justfile"):
        path = root / name
        if path.is_file() and not path.is_symlink():
            text = path.read_bytes()[:200_000].decode("utf-8", errors="ignore")
            return list(dict.fromkeys(m.group(1) for m in JUST_RECIPE_RE.finditer(text)))[:limit]
    return []


def _collect_ci_run_steps(root: Path, limit: int) -> list[str]:
    """读取 GitHub Actions 中真实执行的 run 命令：这是“哪些测试真的在跑”的客观依据。"""
    steps: list[str] = []
    workflow_dir = root / ".github" / "workflows"
    if not workflow_dir.is_dir():
        return steps
    for wf in sorted(workflow_dir.iterdir()):
        if wf.suffix.lower() not in {".yml", ".yaml"} or not wf.is_file() or wf.is_symlink():
            continue
        lines = wf.read_bytes()[:200_000].decode("utf-8", errors="ignore").splitlines()
        i = 0
        while i < len(lines):
            m = CI_RUN_KEY_RE.match(lines[i])
            if not m:
                i += 1
                continue
            indent, value = len(m.group(1)), m.group(2).strip()
            commands: list[str] = []
            i += 1
            if value[:1] in {"|", ">"}:
                while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
                    cmd = lines[i].strip()
                    if cmd and not cmd.startswith("#"):
                        commands.append(cmd)
                    i += 1
            elif value:
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                commands.append(value)
            for cmd in commands:
                steps.append(f"{wf.name}: {cmd}")
                if len(steps) >= limit:
                    return steps
    return steps

# ---- 项目开发约定（oil-dev）解析：三个脚本各带一份逐字相同的副本，由仓库级测试校验 ----
DEV_PROFILE_FILES = ("AGENTS.md", "CLAUDE.md")
DEV_PROFILE_KEYS = {
    "快测": "quick_test",
    "快测命令": "quick_test",
    "quick-test": "quick_test",
    "重测": "full_test",
    "重测命令": "full_test",
    "full-test": "full_test",
    "门禁": "merge_gate",
    "合并门禁": "merge_gate",
    "merge-gate": "merge_gate",
    "高风险": "high_risk",
    "高风险目录": "high_risk",
    "high-risk": "high_risk",
    "禁改": "do_not_edit",
    "禁改目录": "do_not_edit",
    "do-not-edit": "do_not_edit",
}
DEV_PROFILE_LABELS = {
    "quick_test": "快测",
    "full_test": "重测",
    "merge_gate": "门禁",
    "high_risk": "高风险",
    "do_not_edit": "禁改",
}
DEV_PROFILE_GLOB_KEYS = {"high_risk", "do_not_edit"}
DEV_PROFILE_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
DEV_PROFILE_ITEM_RE = re.compile(r"^\s*[-*+]\s+([^:：]+?)\s*[:：]\s*(.*)$")
DEV_PROFILE_CODE_RE = re.compile(r"`([^`]+)`")


def _load_dev_profile(root: Path) -> dict[str, Any] | None:
    """读取 AGENTS.md（或 CLAUDE.md）里标题含 oil-dev 的开发约定小节；没有这一节时返回 None。"""
    for name in DEV_PROFILE_FILES:
        path = root / name
        if not path.is_file() or path.is_symlink():
            continue
        lines = path.read_bytes()[:200_000].decode("utf-8", errors="ignore").splitlines()
        level = 0
        start_line = 0
        in_fence = False
        values: dict[str, list[str]] = {}
        for line_no, line in enumerate(lines, start=1):
            if line.lstrip().startswith(("```", "~~~")):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            heading = DEV_PROFILE_HEADING_RE.match(line)
            if heading:
                depth = len(heading.group(1))
                if start_line and depth <= level:
                    break
                if not start_line and "oil-dev" in heading.group(2).lower():
                    level, start_line = depth, line_no
                continue
            item = DEV_PROFILE_ITEM_RE.match(line) if start_line else None
            if not item:
                continue
            key = DEV_PROFILE_KEYS.get(re.sub(r"[*`_]", "", item.group(1)).strip().lower())
            if not key:
                continue
            raw = item.group(2).strip()
            found = DEV_PROFILE_CODE_RE.findall(raw)
            if not found and raw:
                found = re.split(r"[、,，]", raw) if key in DEV_PROFILE_GLOB_KEYS else [raw]
            values.setdefault(key, []).extend(v.strip() for v in found if v.strip())
        if start_line:
            return {"source": f"{name}:{start_line}", **values}
    return None


def _run_git(root: Path, args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotePath=false", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return proc.returncode, proc.stdout
    except OSError:
        return 1, ""


def _list_repo_files(root: Path, max_files: int = 30_000) -> tuple[list[str], bool]:
    rc, out = _run_git(root, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    if rc == 0 and out:
        items = sorted(set(p for p in out.split("\0") if p and not p.endswith(".DS_Store")))
        files = [p for p in items if (root / p).is_file() and not (root / p).is_symlink()]
        return files[:max_files], len(files) > max_files

    collected: list[str] = []
    for cur, dirs, filenames in os.walk(root):
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and d.lower() not in FALLBACK_SKIP_DIRS
        )
        for name in sorted(filenames):
            if name == ".DS_Store":
                continue
            full = Path(cur) / name
            if full.is_symlink():
                continue
            collected.append(full.relative_to(root).as_posix())
            if len(collected) >= max_files:
                return collected, True
    return collected, False


def _read_text(path: Path, max_bytes: int = 200_000) -> str:
    try:
        return path.read_bytes()[:max_bytes].decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _collect_defined_entries(root: Path, rel_files: list[str]) -> dict[str, list[str]]:
    entries: dict[str, list[str]] = {}

    pkg_path = root / "package.json"
    if pkg_path.is_file():
        try:
            pkg = json.loads(_read_text(pkg_path))
        except json.JSONDecodeError:
            pkg = {}
        scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
        if isinstance(scripts, dict) and scripts:
            entries["package_scripts"] = _prioritized_package_scripts(scripts, 15)

    makefile = root / "Makefile"
    if makefile.is_file():
        targets = [
            m.group(1)
            for m in re.finditer(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", _read_text(makefile), re.MULTILINE)
            if not m.group(1).startswith(".")
        ]
        if targets:
            entries["make_targets"] = list(dict.fromkeys(targets))[:15]

    repo_scripts = [
        p
        for p in rel_files
        if p.startswith("scripts/")
        and Path(p).parent == Path("scripts")
        and (os.access(root / p, os.X_OK) or Path(p).suffix.lower() in {".sh", ".py", ".js", ".mjs", ".ts", ".ps1"})
    ]
    if repo_scripts:
        entries["scripts_dir"] = repo_scripts[:25]

    just_recipes = _collect_just_recipes(root, 15)
    if just_recipes:
        entries["just_recipes"] = just_recipes

    ci_steps = _collect_ci_run_steps(root, 20)
    if ci_steps:
        entries["ci_run_steps"] = ci_steps

    return entries


def _collect_worktrees(root: Path) -> list[dict[str, str]]:
    rc, out = _run_git(root, ["worktree", "list", "--porcelain"])
    if rc != 0 or not out.strip():
        return []
    worktrees: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            if cur.get("path"):
                worktrees.append(cur)
            cur = {}
        elif line.startswith("worktree "):
            cur["path"] = line[9:].strip()
        elif line.startswith("branch "):
            cur["branch"] = line[7:].strip().removeprefix("refs/heads/")
        elif line == "detached":
            cur["branch"] = "detached"
    if cur.get("path"):
        worktrees.append(cur)
    return worktrees


def scan_repository(workspace: Path, max_depth: int = 2) -> dict[str, Any]:
    root = workspace.expanduser().resolve()
    if not root.is_dir():
        return {
            "contract_version": CONTRACT_VERSION,
            "status": "ERROR",
            "error": f"工作区目录不存在或不可访问：{workspace}",
        }

    rel_files, truncated = _list_repo_files(root)
    ext_counter: Counter[str] = Counter()
    dir_counter: Counter[str] = Counter()

    for rel in rel_files:
        parts = Path(rel).parts
        ext = Path(rel).suffix.lower() or "(no-ext)"
        ext_counter[ext] += 1
        if len(parts) > 1:
            key = "/".join(parts[: min(max_depth, len(parts) - 1)])
            dir_counter[key] += 1

    docs_info: list[dict[str, Any]] = []
    for name in ROOT_DOC_CANDIDATES:
        p = root / name
        if p.is_file():
            text = _read_text(p, max_bytes=512_000)
            docs_info.append(
                {
                    "path": name,
                    "lines": len(text.splitlines()),
                    "bytes": p.stat().st_size,
                }
            )
    docs_md = [p for p in rel_files if p.startswith("docs/") and p.endswith(".md")]
    if docs_md:
        docs_info.append({"path": "docs/", "markdown_files": len(docs_md), "sample_files": docs_md[:8]})

    manifests = [name for name in MANIFEST_CANDIDATES if (root / name).is_file()]
    entries = _collect_defined_entries(root, rel_files)
    worktrees = _collect_worktrees(root)

    return {
        "contract_version": CONTRACT_VERSION,
        "status": "OK",
        "workspace": str(root),
        "truncated": truncated,
        "total_files": len(rel_files),
        "extensions": [{"ext": ext, "files": count} for ext, count in ext_counter.most_common(10)],
        "manifests": manifests,
        "defined_entries": entries,
        "governance_files": docs_info,
        "dev_profile": _load_dev_profile(root),
        "worktrees": worktrees,
        "directory_overview": [
            {"dir": directory, "files": count} for directory, count in dir_counter.most_common(16)
        ],
    }


def _print_human(report: dict[str, Any]) -> None:
    if report.get("status") != "OK":
        print(f"[ERROR] {report.get('error')}")
        return

    print(f"# 仓库结构快照：{report['workspace']}")
    print(f"- 文件总数：{report['total_files']}（已遵循 Git 忽略规则）")
    if report["extensions"]:
        exts = ", ".join(f"{x['ext']} ({x['files']})" for x in report["extensions"][:8])
        print(f"- 文件后缀分布：{exts}")
    if report["manifests"]:
        print(f"- 工程清单：{', '.join(report['manifests'])}")
    if len(report["worktrees"]) > 1:
        wts = ", ".join(f"{Path(w['path']).name}({w.get('branch', '?')})" for w in report["worktrees"][:6])
        print(f"- 并行 Worktree：共 {len(report['worktrees'])} 个（{wts}）")

    if report["governance_files"]:
        print("\n## 规范与文档入口")
        for item in report["governance_files"]:
            if "lines" in item:
                print(f"- `{item['path']}`（{item['lines']} 行，{round(item['bytes'] / 1024, 1)} KB）")
            else:
                print(f"- `{item['path']}`（{item['markdown_files']} 篇 Markdown）")

    profile = report.get("dev_profile")
    print("\n## 项目开发约定")
    if profile:
        print(f"- 来源：`{profile['source']}`（以下命令与目录以它为准）")
        for key, label in DEV_PROFILE_LABELS.items():
            if profile.get(key):
                print(f"- {label}：{', '.join(f'`{v}`' for v in profile[key])}")
    else:
        print("- 未找到（AGENTS.md 中标题含 oil-dev 的小节）；测试命令需从下方脚本与 CI 中确认，可用 oil-govern 生成")

    if report["directory_overview"]:
        print("\n## 主要目录规模")
        for d in report["directory_overview"]:
            print(f"- `{d['dir']}/`（{d['files']} 个文件）")

    if report["defined_entries"]:
        print("\n## 仓库内已定义的脚本与入口")
        for group, items in report["defined_entries"].items():
            print(f"- {group}: {', '.join(f'`{i}`' for i in items[:25])}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="快速输出代码库目录规模、规范入口、真实脚本列表与 Worktree 状态")
    parser.add_argument("--workspace", default=".", help="目标仓库路径（默认当前目录）")
    parser.add_argument("--max-depth", type=int, default=2, help="目录聚合深度（默认 2）")
    parser.add_argument("--json", action="store_true", dest="as_json", help="输出结构化 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = scan_repository(Path(args.workspace), max_depth=max(1, args.max_depth))
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 0 if report.get("status") == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
