#!/usr/bin/env python3
"""采集仓库客观治理事实：文档相对死链、规范体积与新鲜度、Git 演进温差、大文件、测试入口与 Worktree 状态（仅用标准库）。"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import unquote

if sys.version_info < (3, 10):
    sys.stderr.write("错误：需要 Python 3.10 或更高版本。\n")
    raise SystemExit(2)

CONTRACT_VERSION = 1

FALLBACK_SKIP_DIRS = {"node_modules", "vendor", "dist", "build", "target", "out", "__pycache__", "coverage"}
DATA_OR_DOC_EXTS = {".md", ".mdx", ".json", ".lock", ".yaml", ".yml", ".toml", ".svg", ".txt", ".csv", ".strings", ".stringsdict"}
MD_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
CHURN_COMMIT_WINDOW = 80
DOC_AGE_COMMIT_WINDOW = 2000
FIRST_PARENT_WINDOW = 5000
DEFAULT_PATCH_ID_WINDOW = 300
MAX_BRANCHES_CHECKED = 50
MERGED_STATES = {"merged", "squash_merged", "rebase_merged"}
PATCH_DIFF_ARGS = ["--no-color", "--no-ext-diff", "--find-renames", "--src-prefix=a/", "--dst-prefix=b/"]

# ---- 测试与构建入口发现（与 oil-explore/scripts/repo_map.py 保持逐字一致，由仓库级测试校验） ----
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


def _matches_any(path: str, patterns: list[str]) -> bool:
    """`*` 与 `**` 都可跨目录匹配；以 `/` 结尾的模式表示整个目录。"""
    for pattern in patterns:
        normalized = pattern.strip().removeprefix("./")
        if not normalized:
            continue
        if normalized.endswith("/"):
            normalized += "**"
        if fnmatch.fnmatchcase(path, normalized) or path == normalized:
            return True
    return False


# ---- 通用工具 ----


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


def _run_git_bytes(root: Path, args: list[str], stdin: bytes | None = None) -> tuple[int, bytes]:
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotePath=false", *args],
            cwd=str(root),
            input=stdin,
            capture_output=True,
            check=False,
        )
        return proc.returncode, proc.stdout
    except OSError:
        return 1, b""


def _read_text(path: Path, max_bytes: int = 400_000) -> str:
    try:
        return path.read_bytes()[:max_bytes].decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _list_repo_files(root: Path, max_files: int = 30_000) -> list[str]:
    rc, out = _run_git(root, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    if rc == 0 and out:
        items = sorted(set(p for p in out.split("\0") if p and not p.endswith(".DS_Store")))
        return [p for p in items if (root / p).is_file() and not (root / p).is_symlink()][:max_files]

    collected: list[str] = []
    for cur, dirs, filenames in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d.lower() not in FALLBACK_SKIP_DIRS)
        for name in sorted(filenames):
            if name == ".DS_Store":
                continue
            full = Path(cur) / name
            if not full.is_symlink():
                collected.append(full.relative_to(root).as_posix())
                if len(collected) >= max_files:
                    return collected
    return collected


def _collect_git_churn(root: Path, max_commits: int = CHURN_COMMIT_WINDOW) -> Counter[str]:
    rc, out = _run_git(root, ["log", f"-n{max_commits}", "--name-only", "--pretty=format:__COMMIT__"])
    if rc != 0 or not out:
        return Counter()

    churn: Counter[str] = Counter()
    for raw in out.splitlines():
        line = raw.strip()
        if line and line != "__COMMIT__":
            churn[line] += 1
    return churn


def _collect_doc_ages(root: Path) -> dict[str, dict[str, int | None]]:
    """每篇 Markdown 最近一次修改距今的提交数与天数（不受近期活跃度采样窗口限制）。"""
    rc, rev_out = _run_git(root, ["rev-list", f"--max-count={DOC_AGE_COMMIT_WINDOW}", "HEAD"])
    if rc != 0:
        return {}
    position = {sha: idx for idx, sha in enumerate(rev_out.split())}
    rc, log_out = _run_git(
        root,
        [
            "log",
            f"--max-count={DOC_AGE_COMMIT_WINDOW}",
            "--format=__COMMIT__%H %ct",
            "--name-only",
            "--",
            "*.md",
            "*.MD",
            "*.mdx",
        ],
    )
    if rc != 0:
        return {}

    now = time.time()
    ages: dict[str, dict[str, int | None]] = {}
    sha: str | None = None
    ts: int | None = None
    for raw in log_out.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("__COMMIT__"):
            parts = line[len("__COMMIT__") :].split()
            sha = parts[0] if parts else None
            ts = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            continue
        if sha is None or line in ages:
            continue
        ages[line] = {
            "commits_since_touch": position.get(sha),
            "days_since_touch": int((now - ts) // 86400) if ts else None,
        }
    return ages


def _doc_age_fields(rel: str, ages: dict[str, dict[str, int | None]]) -> dict[str, Any]:
    age = ages.get(rel)
    if age is None:
        return {"commits_since_touch": None, "days_since_touch": None, "commits_beyond_window": False}
    return {
        "commits_since_touch": age["commits_since_touch"],
        "days_since_touch": age["days_since_touch"],
        "commits_beyond_window": age["commits_since_touch"] is None,
    }


def _describe_age(entry: dict[str, Any]) -> str:
    days = entry.get("days_since_touch")
    day_part = f" / {days} 天" if days is not None else ""
    if entry.get("commits_since_touch") is not None:
        return f"，距最近修改 {entry['commits_since_touch']} 次提交{day_part}"
    if entry.get("commits_beyond_window"):
        return f"，超过 {DOC_AGE_COMMIT_WINDOW} 次提交未修改{day_part}"
    return "，无提交记录（未跟踪或尚未提交）"


def _check_markdown_links(root: Path, rel_md: str) -> list[dict[str, Any]]:
    doc_path = root / rel_md
    lines = _read_text(doc_path).splitlines()
    broken: list[dict[str, Any]] = []
    in_fence = False

    for line_no, raw_line in enumerate(lines, start=1):
        if re.match(r"^\s*(`{3,}|~{3,})", raw_line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in MD_LINK_RE.finditer(raw_line):
            target = m.group(1).strip().split()[0].strip("<>")
            if not target or target.startswith(("#", "http://", "https://", "mailto:", "tel:", "file://", "data:")):
                continue
            clean = unquote(target.split("#", 1)[0].split("?", 1)[0])
            if not clean or any(ch in clean for ch in ("*", "{", "}", "<", ">", "$")):
                continue
            resolved = (doc_path.parent / clean).resolve()
            if not resolved.exists():
                broken.append({"path": rel_md, "line": line_no, "target": clean})
    return broken


def _audit_docs(root: Path, rel_files: list[str], ages: dict[str, dict[str, int | None]]) -> dict[str, Any]:
    root_mds = [p for p in rel_files if "/" not in p and p.lower().endswith(".md")]
    docs_mds = [p for p in rel_files if p.startswith("docs/") and p.lower().endswith(".md")]

    rule_files_summary: list[dict[str, Any]] = []
    for rel in root_mds:
        full = root / rel
        text = _read_text(full)
        rule_files_summary.append(
            {
                "path": rel,
                "lines": len(text.splitlines()),
                "bytes": full.stat().st_size,
                **_doc_age_fields(rel, ages),
                "uses_at_agents_ref": "@AGENTS.md" in text.splitlines()[:3],
            }
        )

    broken_in_root: list[dict[str, Any]] = []
    for rel in root_mds:
        broken_in_root.extend(_check_markdown_links(root, rel))

    broken_in_docs: list[dict[str, Any]] = []
    docs_inventory: list[dict[str, Any]] = []
    for rel in docs_mds:
        full = root / rel
        broken_links = _check_markdown_links(root, rel)
        broken_in_docs.extend(broken_links)
        docs_inventory.append(
            {
                "path": rel,
                "lines": len(_read_text(full).splitlines()),
                **_doc_age_fields(rel, ages),
                "broken_links": len(broken_links),
            }
        )
    # 最久未修改的文档排在前面，方便优先核对是否过期
    docs_inventory.sort(
        key=lambda d: (
            -(d["days_since_touch"] if d["days_since_touch"] is not None else -1),
            d["path"],
        )
    )

    profile = _load_dev_profile(root)
    profile_missing: list[str] = []
    profile_unmatched: list[str] = []
    if profile:
        profile_missing = [k for k in ("quick_test", "merge_gate", "high_risk") if not profile.get(k)]
        # 只查高风险目录：规则失效会让深审分流悄悄失灵；禁改目录常是被忽略的生成目录，查了会误报
        for pattern in profile.get("high_risk", []):
            if not any(_matches_any(rel, [pattern]) for rel in rel_files):
                profile_unmatched.append(pattern)

    return {
        "dev_profile": profile,
        "dev_profile_missing_keys": profile_missing,
        "dev_profile_unmatched_patterns": profile_unmatched,
        "root_markdown_files": rule_files_summary,
        "docs_count": len(docs_mds),
        "broken_links_root": broken_in_root,
        "broken_links_docs": broken_in_docs[:40],
        "broken_links_docs_total": len(broken_in_docs),
        "docs_sample": docs_inventory[:25],
    }


def _audit_code_and_tests(root: Path, rel_files: list[str], churn: Counter[str]) -> dict[str, Any]:
    top_dir_churn: Counter[str] = Counter()
    for path_str, count in churn.items():
        parts = Path(path_str).parts
        top = parts[0] if len(parts) > 1 else "(root)"
        top_dir_churn[top] += count

    large_files: list[dict[str, Any]] = []
    for rel in rel_files:
        ext = Path(rel).suffix.lower()
        if ext in DATA_OR_DOC_EXTS or not ext:
            continue
        full = root / rel
        try:
            if full.stat().st_size < 3_000:
                continue
            raw = full.read_bytes()[:600_000]
        except OSError:
            continue
        if b"\x00" in raw[:8192]:
            continue
        line_count = len(raw.decode("utf-8", errors="ignore").splitlines())
        if line_count > 400:
            large_files.append(
                {
                    "path": rel,
                    "lines": line_count,
                    "recent_commits": churn.get(rel, 0),
                }
            )
    large_files.sort(key=lambda item: (-item["recent_commits"], -item["lines"], item["path"]))

    defined_scripts: dict[str, list[str]] = {}
    pkg_path = root / "package.json"
    if pkg_path.is_file():
        try:
            pkg = json.loads(_read_text(pkg_path))
        except json.JSONDecodeError:
            pkg = {}
        scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
        if isinstance(scripts, dict) and scripts:
            defined_scripts["package_json"] = _prioritized_package_scripts(scripts, 20)

    makefile = root / "Makefile"
    if makefile.is_file():
        targets = [
            m.group(1)
            for m in re.finditer(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", _read_text(makefile), re.MULTILINE)
            if not m.group(1).startswith(".")
        ]
        if targets:
            defined_scripts["makefile"] = list(dict.fromkeys(targets))[:20]

    just_recipes = _collect_just_recipes(root, 20)
    if just_recipes:
        defined_scripts["justfile"] = just_recipes

    scripts_dir_files = [
        p
        for p in rel_files
        if p.startswith("scripts/")
        and Path(p).parent == Path("scripts")
        and (os.access(root / p, os.X_OK) or Path(p).suffix.lower() in {".sh", ".py", ".js", ".mjs", ".ts", ".ps1"})
    ]
    if scripts_dir_files:
        defined_scripts["scripts_dir"] = scripts_dir_files[:25]

    ci_steps = _collect_ci_run_steps(root, 30)
    if ci_steps:
        defined_scripts["ci_run_steps"] = ci_steps

    test_dirs: Counter[str] = Counter()
    for rel in rel_files:
        parts = Path(rel).parts
        if len(parts) >= 2 and any("test" in seg.lower() or "e2e" in seg.lower() for seg in parts[:-1]):
            bucket = "/".join(parts[: min(2, len(parts) - 1)])
            test_dirs[bucket] += 1

    return {
        "top_dir_recent_commits": [{"dir": d, "commits": c} for d, c in top_dir_churn.most_common(10)],
        "large_files_over_400_lines": large_files[:20],
        "large_files_total": len(large_files),
        "test_directories": [{"dir": d, "files": c} for d, c in test_dirs.most_common(15)],
        "defined_scripts": defined_scripts,
        "has_ci_config": bool(ci_steps) or any(
            (root / p).exists() for p in (".gitlab-ci.yml", ".circleci", "azure-pipelines.yml", "Jenkinsfile")
        ),
    }


# ---- 分支与 Worktree 合入状态 ----


def _first_token_set(raw: bytes) -> set[str]:
    return {line.split()[0] for line in raw.decode("utf-8", errors="replace").splitlines() if line.strip()}


def _default_branch_context(root: Path, default_branch: str) -> dict[str, Any]:
    _, tip = _run_git(root, ["rev-parse", default_branch])
    _, fp_out = _run_git(root, ["rev-list", "--first-parent", f"--max-count={FIRST_PARENT_WINDOW}", default_branch])
    _, log_patch = _run_git_bytes(
        root,
        ["log", "-p", "--no-merges", "--format=commit %H", f"--max-count={DEFAULT_PATCH_ID_WINDOW}", *PATCH_DIFF_ARGS, default_branch],
    )
    _, pid_out = _run_git_bytes(root, ["patch-id", "--stable"], stdin=log_patch) if log_patch else (0, b"")
    return {
        "name": default_branch,
        "tip": tip.strip(),
        "first_parent": set(fp_out.split()),
        "patch_ids": _first_token_set(pid_out),
    }


def _branch_merge_state(root: Path, branch: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """区分普通合并、squash 合并、rebase 合并、无独立提交与未合入，避免把刚建的分支误判为已合入。"""
    default_branch = ctx["name"]
    rc, counts = _run_git(root, ["rev-list", "--left-right", "--count", f"{default_branch}...{branch}"])
    parts = counts.split()
    if rc != 0 or len(parts) != 2:
        return {"merge_state": "unknown", "ahead": None, "behind": None}
    behind, ahead = int(parts[0]), int(parts[1])
    _, tip = _run_git(root, ["rev-parse", branch])
    tip = tip.strip()

    if ahead == 0:
        # 分支末端就在主干自己的 first-parent 线上：可能刚建还没提交，也可能已快进合并，无法客观区分
        state = "no_own_commits" if tip in ctx["first_parent"] else "merged"
        return {"merge_state": state, "ahead": ahead, "behind": behind}

    _, cherry_out = _run_git(root, ["cherry", default_branch, branch])
    cherry_lines = [ln for ln in cherry_out.splitlines() if ln.strip()]
    if cherry_lines and all(ln.startswith("-") for ln in cherry_lines):
        return {"merge_state": "rebase_merged", "ahead": ahead, "behind": behind}

    rc_mb, mb = _run_git(root, ["merge-base", default_branch, branch])
    if rc_mb == 0 and mb.strip() and ctx["patch_ids"]:
        _, combined = _run_git_bytes(root, ["diff", *PATCH_DIFF_ARGS, mb.strip(), branch])
        if combined:
            _, pid_out = _run_git_bytes(root, ["patch-id", "--stable"], stdin=combined)
            if _first_token_set(pid_out) & ctx["patch_ids"]:
                return {"merge_state": "squash_merged", "ahead": ahead, "behind": behind}

    return {"merge_state": "unmerged", "ahead": ahead, "behind": behind}


def _local_branches(root: Path) -> dict[str, bool]:
    """本地分支 -> 远端跟踪分支是否已被删除（常见于 PR 合并后自动删分支）。"""
    rc, out = _run_git(root, ["for-each-ref", "--format=%(refname:short)%00%(upstream:track)", "refs/heads"])
    branches: dict[str, bool] = {}
    if rc != 0:
        return branches
    for line in out.splitlines():
        name, _, track = line.partition("\0")
        if name:
            branches[name] = "gone" in track
    return branches


def _audit_worktrees(root: Path) -> dict[str, Any]:
    rc, out = _run_git(root, ["worktree", "list", "--porcelain"])
    if rc != 0 or not out.strip():
        return {"worktrees": [], "merged_local_branches": []}

    default_branch = ""
    for cand in ("main", "master"):
        if _run_git(root, ["rev-parse", "--verify", "--quiet", cand])[0] == 0:
            default_branch = cand
            break

    worktrees: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
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

    branches = _local_branches(root)
    ctx = _default_branch_context(root, default_branch) if default_branch else None
    state_cache: dict[str, dict[str, Any]] = {}

    def state_of(branch: str) -> dict[str, Any]:
        if branch not in state_cache:
            state_cache[branch] = _branch_merge_state(root, branch, ctx) if ctx else {
                "merge_state": "unknown",
                "ahead": None,
                "behind": None,
            }
        return state_cache[branch]

    for idx, wt in enumerate(worktrees):
        wt_path = Path(wt["path"]).resolve()
        wt["is_main_worktree"] = idx == 0
        wt["nested_inside_workspace"] = wt_path != root and root in wt_path.parents
        rc_st, st_out = _run_git(wt_path, ["status", "--porcelain"]) if wt_path.is_dir() else (1, "")
        wt["uncommitted_entries"] = len([ln for ln in st_out.splitlines() if ln.strip()]) if rc_st == 0 else 0
        branch = wt.get("branch", "")
        if branch == "detached" or not branch:
            wt.update({"merge_state": "detached", "ahead": None, "behind": None})
        elif branch == default_branch:
            wt.update({"merge_state": "default_branch", "ahead": 0, "behind": 0})
        else:
            wt.update(state_of(branch))
        wt["upstream_gone"] = branches.get(branch, False)
        wt["merged_into_default"] = wt["merge_state"] in MERGED_STATES
        wt["cleanup_candidate"] = (
            not wt["is_main_worktree"] and wt["merged_into_default"] and wt["uncommitted_entries"] == 0
        )

    merged_branches: list[str] = []
    gone_branches: list[str] = []
    candidates = [b for b in branches if b != default_branch]
    for branch in candidates[:MAX_BRANCHES_CHECKED]:
        if ctx and state_of(branch)["merge_state"] in MERGED_STATES:
            merged_branches.append(f"{branch}({state_of(branch)['merge_state']})")
        if branches[branch]:
            gone_branches.append(branch)

    return {
        "default_branch": default_branch,
        "worktrees": worktrees,
        "merged_local_branches": merged_branches[:20],
        "merged_local_branches_total": len(merged_branches),
        "upstream_gone_branches": gone_branches[:20],
        "branches_checked": min(len(candidates), MAX_BRANCHES_CHECKED),
        "branches_truncated": len(candidates) > MAX_BRANCHES_CHECKED,
    }


def run_governance_audit(workspace: Path, scope: str = "all") -> dict[str, Any]:
    root = workspace.expanduser().resolve()
    if not root.is_dir():
        return {"contract_version": CONTRACT_VERSION, "status": "ERROR", "error": f"找不到目录：{workspace}"}

    rel_files = _list_repo_files(root)
    churn = _collect_git_churn(root) if scope in {"all", "tests"} else Counter()
    ages = _collect_doc_ages(root) if scope in {"all", "docs"} else {}

    return {
        "contract_version": CONTRACT_VERSION,
        "status": "OK",
        "workspace": str(root),
        "scope": scope,
        "docs": _audit_docs(root, rel_files, ages) if scope in {"all", "docs"} else None,
        "code_and_tests": _audit_code_and_tests(root, rel_files, churn) if scope in {"all", "tests"} else None,
        "worktrees": _audit_worktrees(root) if scope in {"all", "worktrees"} else None,
    }


MERGE_STATE_LABELS = {
    "merged": "已合入 {default}（普通合并）",
    "squash_merged": "已 squash 合入 {default}",
    "rebase_merged": "已 rebase/squash 合入 {default}",
    "no_own_commits": "没有独立提交（刚建未开工或已快进合并，清理前需确认）",
}


def _print_human(report: dict[str, Any]) -> None:
    if report.get("status") != "OK":
        print(f"[ERROR] {report.get('error')}")
        return

    print(f"# 仓库治理事实快照：{report['workspace']}")

    docs = report.get("docs")
    if docs:
        print(f"\n## 一、规范与文档（根目录 Markdown {len(docs['root_markdown_files'])} 篇，docs/ 下 {docs['docs_count']} 篇）")
        for r in docs["root_markdown_files"]:
            kb = round(r["bytes"] / 1024, 1)
            ref_tag = " [@AGENTS.md 单源引用]" if r["uses_at_agents_ref"] else ""
            print(f"- `{r['path']}`：{r['lines']} 行 / {kb} KB{_describe_age(r)}{ref_tag}")
        for b in docs["broken_links_root"]:
            print(f"- [死链] `{b['path']}:{b['line']}` -> `{b['target']}`")
        profile = docs.get("dev_profile")
        if profile:
            missing = "、".join(DEV_PROFILE_LABELS[k] for k in docs["dev_profile_missing_keys"])
            missing_note = f"，缺少：{missing}" if missing else ""
            print(f"- 开发约定：`{profile['source']}`{missing_note}")
            if docs["dev_profile_unmatched_patterns"]:
                stale = ", ".join(f"`{p}`" for p in docs["dev_profile_unmatched_patterns"])
                print(f"- 开发约定里匹配不到任何文件的高风险目录（可能已改名或删除）：{stale}")
        else:
            print("- 未找到开发约定（AGENTS.md 中标题含 oil-dev 的小节）：建议补上快测、门禁、高风险与禁改目录")
        stale_docs = [d for d in docs["docs_sample"] if d["days_since_touch"] is not None][:5]
        if stale_docs:
            print("- `docs/` 中最久未修改：" + "，".join(f"`{d['path']}`（{d['days_since_touch']} 天）" for d in stale_docs))
        if docs["broken_links_docs_total"]:
            print(f"- `docs/` 相对链接失效：共 {docs['broken_links_docs_total']} 处")
            for b in docs["broken_links_docs"][:10]:
                print(f"  - `{b['path']}:{b['line']}` -> `{b['target']}`")

    ct = report.get("code_and_tests")
    if ct:
        print("\n## 二、代码规模、近期演进与测试入口")
        if ct["top_dir_recent_commits"]:
            hot = ", ".join(f"`{x['dir']}` ({x['commits']})" for x in ct["top_dir_recent_commits"][:6])
            print(f"- 近 {CHURN_COMMIT_WINDOW} 次提交活跃目录：{hot}")
        if ct["test_directories"]:
            tdirs = ", ".join(f"`{x['dir']}` ({x['files']})" for x in ct["test_directories"][:8])
            print(f"- 测试目录分布：{tdirs}")
        if ct["defined_scripts"]:
            for k, items in ct["defined_scripts"].items():
                print(f"- 已定义脚本（{k}）：{', '.join(f'`{i}`' for i in items[:25])}")
        if not ct.get("has_ci_config"):
            print("- 未发现 CI 配置：无法客观确认哪些测试真的会被执行")
        if ct["large_files_total"]:
            print(f"- 超过 400 行的代码文件：共 {ct['large_files_total']} 个")
            for lf in ct["large_files_over_400_lines"][:8]:
                print(f"  - `{lf['path']}`（{lf['lines']} 行，近期改动 {lf['recent_commits']} 次）")

    wt_info = report.get("worktrees")
    if wt_info and wt_info.get("worktrees"):
        default = wt_info.get("default_branch") or "主干"
        print(f"\n## 三、Git Worktree 与分支状态（共 {len(wt_info['worktrees'])} 个工作树）")
        for wt in wt_info["worktrees"]:
            flags: list[str] = []
            if wt["is_main_worktree"]:
                flags.append("主工作树")
            if wt["nested_inside_workspace"]:
                flags.append("嵌套在主仓库内")
            label = MERGE_STATE_LABELS.get(wt["merge_state"])
            if label:
                flags.append(label.format(default=default))
            elif wt["merge_state"] == "unmerged":
                flags.append(f"领先 {wt['ahead']} 个提交未合入")
            if wt.get("upstream_gone"):
                flags.append("远端分支已删除")
            if wt["uncommitted_entries"]:
                flags.append(f"未提交项 {wt['uncommitted_entries']}（不可直接清理）")
            if wt.get("cleanup_candidate"):
                flags.append("可清理候选")
            flag_str = f"（{', '.join(flags)}）" if flags else ""
            print(f"- `{wt['path']}` [{wt.get('branch', '?')}]{flag_str}")
        if wt_info.get("merged_local_branches_total"):
            sample_b = ", ".join(wt_info["merged_local_branches"][:8])
            print(f"- 已合入 {default} 的本地分支：共 {wt_info['merged_local_branches_total']} 个（{sample_b}）")
        if wt_info.get("upstream_gone_branches"):
            print(f"- 远端已删除的本地分支：{', '.join(wt_info['upstream_gone_branches'][:8])}")
        if wt_info.get("branches_truncated"):
            print(f"- 本地分支过多，仅检查了前 {wt_info['branches_checked']} 个")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="采集仓库文档死链与新鲜度、规范体积、代码演进温差、大文件、测试入口与 Worktree 状态")
    parser.add_argument("--workspace", default=".", help="目标仓库根目录")
    parser.add_argument(
        "--scope",
        choices=("all", "docs", "tests", "worktrees"),
        default="all",
        help="采集范围：all、docs、tests、worktrees",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="输出结构化 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_governance_audit(Path(args.workspace), scope=args.scope)
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 0 if report.get("status") == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
