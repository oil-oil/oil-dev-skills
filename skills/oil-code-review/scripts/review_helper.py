#!/usr/bin/env python3
"""采集待审 Git 改动文件、行号范围与项目现存规范清单（仅使用 Python 3.10+ 标准库）。"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

if sys.version_info < (3, 10):
    sys.stderr.write("错误：需要 Python 3.10 或更高版本。\n")
    raise SystemExit(2)

CONTRACT_VERSION = 1
HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
RULE_CANDIDATES = ("AGENTS.md", "DESIGN.md", "REVIEW.md")
# 固定前缀并关闭路径转义：不受用户 diff.noprefix / diff.mnemonicPrefix / core.quotePath 配置影响
DIFF_COMMON = ["--no-color", "--no-ext-diff", "--find-renames"]
PATCH_PREFIX = ["--src-prefix=a/", "--dst-prefix=b/"]
STATUS_NAMES = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "typechange",
    "U": "unmerged",
}
GIT_ESCAPES = {"n": 10, "t": 9, '"': 34, "\\": 92, "a": 7, "b": 8, "f": 12, "r": 13, "v": 11}
HUMAN_HUNK_LIMIT = 6
JSON_HUNK_LIMIT = 15


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


def _run_git(root: Path, args: list[str]) -> tuple[int, str, str]:
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
        return proc.returncode, proc.stdout, proc.stderr
    except OSError as exc:
        return 1, "", str(exc)


def _count_lines(path: Path) -> int:
    try:
        return len(path.read_bytes()[:512_000].decode("utf-8", errors="ignore").splitlines())
    except OSError:
        return 0


def _unquote_git_path(raw: str) -> str:
    """还原 Git 对含引号、反斜杠或控制字符路径做的 C 风格转义。"""
    raw = raw.rstrip("\t")
    if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
        return raw
    body = raw[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            octal = body[i + 1 : i + 4]
            if len(octal) == 3 and all(c in "01234567" for c in octal):
                out.append(int(octal, 8) & 0xFF)
                i += 4
                continue
            code = GIT_ESCAPES.get(body[i + 1])
            if code is not None:
                out.append(code)
                i += 2
                continue
        out.extend(ch.encode("utf-8"))
        i += 1
    return out.decode("utf-8", errors="replace")


def _parse_hunks(diff_text: str) -> dict[str, list[str]]:
    """只从 unified diff 中解析新文件侧的改动行号区间。"""
    hunks: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in diff_text.splitlines():
        if raw_line.startswith("+++ "):
            target = _unquote_git_path(raw_line[4:])
            current = None if target == "/dev/null" else target.removeprefix("b/")
        elif current is not None and raw_line.startswith("@@ "):
            m = HUNK_HEADER_RE.match(raw_line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2) or "1")
                hunks.setdefault(current, []).append(f"{start}-{start + max(0, count - 1)}")
    return hunks


def _parse_numstat_z(out: str) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    tokens = out.split("\0")
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token:
            i += 1
            continue
        parts = token.split("\t", 2)
        if len(parts) != 3:
            i += 1
            continue
        added, deleted, path = parts
        if path == "" and i + 2 < len(tokens):
            path = tokens[i + 2]
            i += 3
        else:
            i += 1
        binary = added == "-" or deleted == "-"
        stats[path] = {
            "added": 0 if binary else int(added or 0),
            "deleted": 0 if binary else int(deleted or 0),
            "binary": binary,
        }
    return stats


def _parse_name_status_z(out: str) -> dict[str, dict[str, Any]]:
    statuses: dict[str, dict[str, Any]] = {}
    tokens = out.split("\0")
    i = 0
    while i < len(tokens):
        code = tokens[i]
        if not code:
            i += 1
            continue
        letter = code[0]
        if letter in "RC" and i + 2 < len(tokens):
            statuses[tokens[i + 2]] = {"status": STATUS_NAMES[letter], "old_path": tokens[i + 1]}
            i += 3
        elif i + 1 < len(tokens):
            statuses[tokens[i + 1]] = {"status": STATUS_NAMES.get(letter, "modified")}
            i += 2
        else:
            i += 1
    return statuses


def _collect_diff(root: Path, range_args: list[str]) -> dict[str, dict[str, Any]]:
    """文件清单以 -z 输出为准（含删除、重命名、二进制与任意字符路径），行号区间另从 unified diff 补齐。"""
    _, numstat_out, _ = _run_git(root, ["diff", *DIFF_COMMON, "--numstat", "-z", *range_args])
    _, status_out, _ = _run_git(root, ["diff", *DIFF_COMMON, "--name-status", "-z", *range_args])
    _, patch_out, _ = _run_git(root, ["diff", *DIFF_COMMON, *PATCH_PREFIX, "--unified=0", *range_args])

    stats = _parse_numstat_z(numstat_out)
    statuses = _parse_name_status_z(status_out)
    hunks = _parse_hunks(patch_out)

    files: dict[str, dict[str, Any]] = {}
    for path in sorted(set(stats) | set(statuses)):
        info: dict[str, Any] = {"added": 0, "deleted": 0, "hunks": hunks.get(path, [])}
        info.update(stats.get(path, {}))
        info.update(statuses.get(path, {"status": "modified"}))
        files[path] = info
    return files


def _resolve_diff(
    root: Path,
    base: str | None,
    staged: bool,
    include_untracked: bool,
) -> tuple[str | None, dict[str, dict[str, Any]], str | None, bool]:
    collect_untracked = False

    if staged:
        range_args = ["--cached"]
        scope = "staged"
    elif base:
        if ".." in base:
            range_args = [base]
            scope = base
        else:
            rc_check, _, _ = _run_git(root, ["rev-parse", "--verify", "--quiet", base])
            if rc_check != 0:
                return None, {}, f"指定的基准分支或提交不存在：{base}", False
            if base.startswith(("HEAD~", "HEAD^")):
                range_args = [f"{base}..HEAD"]
                scope = f"{base}..HEAD"
            else:
                _, cur_branch, _ = _run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
                rc_mb, mb_out, _ = _run_git(root, ["merge-base", base, "HEAD"])
                merge_base = mb_out.strip() if rc_mb == 0 else ""
                if cur_branch.strip() != base and merge_base:
                    range_args = [merge_base]
                    scope = f"{base}..working-tree"
                    collect_untracked = include_untracked
                else:
                    range_args = [f"{base}...HEAD"]
                    scope = f"{base}...HEAD"
    else:
        rc_head, _, _ = _run_git(root, ["rev-parse", "--verify", "--quiet", "HEAD"])
        range_args = ["HEAD"] if rc_head == 0 else ["--cached"]
        scope = "working-tree"
        collect_untracked = include_untracked

    parsed = _collect_diff(root, range_args)

    if collect_untracked:
        _, untracked_out, _ = _run_git(root, ["ls-files", "-z", "--others", "--exclude-standard"])
        for rel in (p for p in untracked_out.split("\0") if p):
            full = root / rel
            if full.is_file() and not full.is_symlink():
                n = _count_lines(full)
                parsed.setdefault(
                    rel,
                    {
                        "added": n,
                        "deleted": 0,
                        "hunks": [f"1-{max(1, n)}"] if n else [],
                        "status": "untracked",
                        "untracked": True,
                    },
                )

    # 只有工作区确实没有任何改动（含删除与未跟踪文件）时，才回退去审已提交内容
    auto_fallback = False
    if not parsed and not staged and not base:
        for main_name in ("main", "master"):
            if _run_git(root, ["rev-parse", "--verify", "--quiet", main_name])[0] == 0:
                rc_cnt, cnt_out, _ = _run_git(root, ["rev-list", "--count", f"{main_name}..HEAD"])
                if rc_cnt == 0 and int(cnt_out.strip() or "0") > 0:
                    parsed = _collect_diff(root, [f"{main_name}...HEAD"])
                    scope = f"auto:{main_name}...HEAD"
                    auto_fallback = bool(parsed)
                    break
        if not parsed and _run_git(root, ["rev-parse", "--verify", "--quiet", "HEAD~1"])[0] == 0:
            parsed = _collect_diff(root, ["HEAD~1..HEAD"])
            if parsed:
                scope = "auto:HEAD~1..HEAD"
                auto_fallback = True

    return scope, parsed, None, auto_fallback


def collect_review_context(
    workspace: Path,
    base: str | None = None,
    staged: bool = False,
    include_untracked: bool = True,
    allowed: list[str] | None = None,
) -> dict[str, Any]:
    root = workspace.expanduser().resolve()
    if not root.is_dir():
        return {"contract_version": CONTRACT_VERSION, "status": "ERROR", "error": f"找不到目录：{workspace}"}

    rc, _, err = _run_git(root, ["rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        return {"contract_version": CONTRACT_VERSION, "status": "ERROR", "error": f"不是 Git 仓库：{err.strip() or root}"}

    scope, parsed, diff_err, auto_fallback = _resolve_diff(root, base, staged, include_untracked)
    if diff_err or scope is None:
        return {"contract_version": CONTRACT_VERSION, "status": "ERROR", "error": diff_err or "无法读取 Git 改动"}

    files: list[dict[str, Any]] = []
    for rel in sorted(parsed):
        info = parsed[rel]
        full = root / rel
        entry: dict[str, Any] = {
            "path": rel,
            "status": info.get("status", "modified"),
            "added": info["added"],
            "deleted": info["deleted"],
            "hunks": info["hunks"][:JSON_HUNK_LIMIT],
            "hunks_total": len(info["hunks"]),
            "total_lines": _count_lines(full) if full.is_file() else 0,
        }
        if info.get("old_path"):
            entry["old_path"] = info["old_path"]
        if info.get("binary"):
            entry["binary"] = True
        if info.get("untracked"):
            entry["untracked"] = True
        files.append(entry)

    rule_files = [
        {"path": name, "lines": _count_lines(root / name)}
        for name in RULE_CANDIDATES
        if (root / name).is_file()
    ]

    report: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "status": "OK",
        "workspace": str(root),
        "scope": scope,
        "auto_fallback": auto_fallback,
        "summary": {
            "changed_files": len(files),
            "deleted_files": sum(1 for f in files if f["status"] == "deleted"),
            "total_added": sum(f["added"] for f in files),
            "total_deleted": sum(f["deleted"] for f in files),
        },
        "rule_files": rule_files,
        "files": files,
    }

    profile = _load_dev_profile(root)
    report["dev_profile"] = profile
    if profile:

        def touched(patterns: list[str]) -> list[str]:
            if not patterns:
                return []
            return [
                f["path"]
                for f in files
                if _matches_any(f["path"], patterns) or (f.get("old_path") and _matches_any(f["old_path"], patterns))
            ]

        report["high_risk_files"] = touched(profile.get("high_risk", []))
        report["do_not_edit_files"] = touched(profile.get("do_not_edit", []))

    if allowed:
        report["allowed_patterns"] = allowed
        report["out_of_scope_files"] = [
            f["path"]
            for f in files
            if not _matches_any(f["path"], allowed)
            and not (f.get("old_path") and _matches_any(f["old_path"], allowed))
        ]
    return report


def _print_human(report: dict[str, Any]) -> None:
    if report.get("status") != "OK":
        print(f"[ERROR] {report.get('error')}")
        return
    s = report["summary"]
    print(f"# 待审改动清单（范围：{report['scope']}）")
    if report.get("auto_fallback"):
        print("- 注意：工作区没有未提交改动，已自动改为审查上面这个已提交范围；如需其他范围请显式传 `--base`。")
    deleted_note = f"，其中删除 {s['deleted_files']} 个" if s.get("deleted_files") else ""
    print(f"- 共 {s['changed_files']} 个文件{deleted_note}，+{s['total_added']} / -{s['total_deleted']} 行")
    if report["rule_files"]:
        rules_str = ", ".join(f"`{r['path']}` ({r['lines']} 行)" for r in report["rule_files"])
        print(f"- 仓库规范文件：{rules_str}")
    profile = report.get("dev_profile")
    if profile:
        quick = profile.get("quick_test")
        quick_note = f"，快测 `{quick[0]}`" if quick else ""
        print(f"- 项目开发约定：`{profile['source']}`{quick_note}")
        if report.get("high_risk_files"):
            risky = report["high_risk_files"]
            print(f"- [高风险目录] {len(risky)} 个文件，按独立深审处理：{', '.join(f'`{p}`' for p in risky[:20])}")
        if report.get("do_not_edit_files"):
            locked = report["do_not_edit_files"]
            print(f"- [禁改目录] {len(locked)} 个文件，必须确认是否允许修改：{', '.join(f'`{p}`' for p in locked[:20])}")
    else:
        print("- 未找到项目开发约定（AGENTS.md 中标题含 oil-dev 的小节），高风险范围按 Skill 默认清单判断")
    if "out_of_scope_files" in report:
        outside = report["out_of_scope_files"]
        if outside:
            print(f"- [超出方案允许范围] {len(outside)} 个文件：{', '.join(f'`{p}`' for p in outside[:20])}")
        else:
            print("- 全部改动都在方案允许的文件范围内")
    if report["files"]:
        print("\n## 改动文件与行号范围")
        tags = {"deleted": " [已删除]", "added": " [新增]", "untracked": " [未跟踪新文件]"}
        for f in report["files"][:60]:
            if f["status"] == "deleted":
                print(f"- `{f['path']}` (-{f['deleted']}){tags['deleted']}")
                continue
            hunks = ", ".join(f["hunks"][:HUMAN_HUNK_LIMIT]) or "-"
            if f["hunks_total"] > HUMAN_HUNK_LIMIT:
                hunks += f" …共 {f['hunks_total']} 处"
            tag = tags.get(f["status"], "")
            if f.get("old_path"):
                tag += f" [由 `{f['old_path']}` 重命名]"
            if f.get("binary"):
                tag += " [二进制]"
            print(
                f"- `{f['path']}` (+{f['added']}/-{f['deleted']}，全文件 {f['total_lines']} 行，改动块：{hunks}){tag}"
            )
        if len(report["files"]) > 60:
            print(f"- ... 其余 {len(report['files']) - 60} 个文件见 `--json` 输出")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="采集待审代码的 Git 改动统计、改动行号区间与现存规范清单")
    parser.add_argument("--workspace", default=".", help="Git 仓库根目录")
    parser.add_argument("--base", help="对比基准分支或提交（如 main、HEAD~1）")
    parser.add_argument("--staged", action="store_true", help="仅采集暂存区改动")
    parser.add_argument("--no-untracked", action="store_true", help="不包含未跟踪的新文件")
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="GLOB",
        help="方案允许修改的文件或通配符（可重复，如 'src/export/**'）；列出不在范围内的改动",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="输出结构化 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "collect":
        argv = argv[1:]
    args = build_parser().parse_args(argv)
    report = collect_review_context(
        Path(args.workspace),
        base=args.base,
        staged=args.staged,
        include_untracked=not args.no_untracked,
        allowed=args.allow or None,
    )
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 0 if report.get("status") == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
