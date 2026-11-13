#!/usr/bin/env python3
"""按隐藏测试给 oil-bug-fix 的一次评估运行打分，输出 grading.json 所需的内容。

用法：python3 grade.py <eval-id> <仓库> <基线提交> <汇报文件> <evals.json> [--output grading.json]
每条要求的检查顺序与 evals.json 里 expectations 的顺序一一对应。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

PRINT_RE = re.compile(r"^\+(?!\+\+).*\bprint\(", re.MULTILINE)
OFFSET_RE = re.compile(r"^\+(?!\+\+).*(timedelta\(\s*hours\s*=\s*-?\d|[+-]\s*\d+\s*\*\s*3600|tzinfo\s*=\s*timezone\(\s*timedelta)", re.MULTILINE)
NONE_GUARD_RE = re.compile(r"^\+(?!\+\+).*(is None|is not None|or \"\"|or ''|if not doc\.title|getattr\()", re.MULTILINE)


def sh(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), cwd=str(repo), capture_output=True, text=True, env=env, check=False)


def py(repo: Path, code: str, tz: str | None = None) -> tuple[bool, str]:
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "PYTHONDONTWRITEBYTECODE": "1"}
    if tz:
        env["TZ"] = tz
    proc = sh(repo, sys.executable, "-c", code, env=env)
    out = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode == 0, (out[-1] if out else f"exit {proc.returncode}")


def changes(repo: Path, base: str) -> tuple[str, list[str]]:
    sh(repo, "git", "add", "-N", ".")
    diff = sh(repo, "git", "diff", base).stdout
    files = [f for f in sh(repo, "git", "diff", "--name-only", base).stdout.splitlines() if f]
    return diff, files


def diff_of(diff: str, predicate) -> str:
    keep, current = [], None
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            current = line.split(" b/", 1)[-1]
        if current and predicate(current):
            keep.append(line)
    return "\n".join(keep)


def tests_pass(repo: Path) -> tuple[bool, str]:
    proc = sh(repo, sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q")
    tail = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode == 0, f"unittest discover：{tail[-1] if tail else proc.returncode}"


def no_prints(diff: str) -> tuple[bool, str]:
    product = diff_of(diff, lambda f: f.endswith(".py") and not f.startswith("tests/"))
    hits = PRINT_RE.findall(product)
    return not hits, "产品代码新增 print：%d 处" % len(hits)


def grade_tz(repo: Path, base: str, report: str) -> list[tuple[bool, str]]:
    diff, files = changes(repo, base)
    checks = []
    cases = [
        ("from report.daily import day_of; assert day_of({ts}) == '{want}', day_of({ts})", [(1772420400, "2026-03-02"), (1772395200, "2026-03-01")]),
        ("from export.csv_export import to_csv; r = to_csv([{{'ts': {ts}, 'amount': 1}}]).splitlines()[1]; assert r.startswith('{want}'), r", [(1772420400, "2026-03-02"), (1772395200, "2026-03-01")]),
        ("from billing.invoice import invoice_period; assert invoice_period({ts}) == '{want}', invoice_period({ts})", [(1775008800, "2026-04"), (1774987200, "2026-03")]),
    ]
    for template, (us, cn) in cases:
        ok_us, out_us = py(repo, template.format(ts=us[0], want=us[1]), "America/Los_Angeles")
        ok_cn, out_cn = py(repo, template.format(ts=cn[0], want=cn[1]), "Asia/Shanghai")
        checks.append((ok_us and ok_cn, f"隐藏测试 美国：{out_us or 'ok'}；中国：{out_cn or 'ok'}"))
    hits = OFFSET_RE.findall(diff)
    checks.append((not hits, f"新增固定小时偏移：{len(hits)} 处"))
    test_diff = diff_of(diff, lambda f: f.startswith("tests/"))
    pinned = bool(re.search(r"^\+.*(tzset|\bTZ\b|America/|Asia/|Europe/|ZoneInfo)", test_diff, re.MULTILINE))
    checks.append((pinned, "测试改动里" + ("有" if pinned else "没有") + "固定时区的代码"))
    checks.append(tests_pass(repo))
    checks.append(no_prints(diff))
    touched = [f for f in files if f in ("AGENTS.md", "REVIEW.md")]
    checks.append((not touched, f"改动的规范文件：{touched or '无'}"))
    found = [p for p in ("daily", "csv_export", "invoice") if p in report]
    checks.append((len(found) == 3, f"汇报提到的位置：{found}"))
    return checks


def grade_crash(repo: Path, base: str, report: str) -> list[tuple[bool, str]]:
    diff, files = changes(repo, base)
    checks = []
    ok, out = py(repo, "from notes.app import open_project; r = open_project('samples/old_project.json'); assert r.startswith('# 旧项目'), r")
    checks.append((ok, f"隐藏测试 旧格式：{out or 'ok'}"))
    ok, out = py(repo, "from notes.app import open_project; r = open_project('samples/new_project.json'); assert r.startswith('# 新项目'), r")
    checks.append((ok, f"隐藏测试 新格式：{out or 'ok'}"))
    render_diff = diff_of(diff, lambda f: f == "notes/render.py")
    guards = NONE_GUARD_RE.findall(render_diff)
    checks.append((not guards, "render.py " + ("未改动" if not render_diff else f"改动里的空值兜底：{len(guards)} 处")))
    parser_tests = diff_of(diff, lambda f: f.startswith("tests/") and "pars" in f)
    v1 = bool(re.search(r'^\+.*("version"\s*:\s*1|meta|old_project)', parser_tests, re.MULTILINE))
    checks.append((v1, "解析器测试" + ("新增了" if v1 else "没有新增") + "旧格式用例"))
    checks.append(tests_pass(repo))
    checks.append(no_prints(diff))
    culprit = sh(repo, "git", "log", "--format=%h %H", "--grep", "统一读取顶层字段", base).stdout.split()
    named = bool(culprit) and (culprit[0][:7] in report or "统一读取顶层字段" in report)
    checks.append((named, f"引入提交 {culprit[0][:7] if culprit else '?'}：汇报" + ("提到了" if named else "没提到")))
    return checks


def grade_label(repo: Path, base: str, report: str) -> list[tuple[bool, str]]:
    diff, files = changes(repo, base)
    ok, out = py(repo, "from ui.labels import label; assert label('save') == '保存', label('save')")
    checks = [(ok, f"隐藏测试：{out or 'ok'}")]
    extra = [f for f in files if f not in ("ui/labels.py", "tests/test_labels.py")]
    checks.append((not extra, f"文案和测试之外的改动：{extra or '无'}"))
    checks.append(tests_pass(repo))
    lines = len([l for l in report.splitlines() if l.strip()])
    bisected = "bisect" in report.lower() or "二分" in report
    checks.append((lines <= 25 and not bisected, f"汇报 {lines} 行，" + ("提到了二分" if bisected else "没有二分")))
    return checks


GRADERS = {1: grade_tz, 2: grade_crash, 3: grade_label}


def main(argv: list[str]) -> int:
    if len(argv) < 6:
        sys.stderr.write(__doc__)
        return 2
    eval_id, repo, base = int(argv[1]), Path(argv[2]), argv[3]
    report = Path(argv[4]).read_text(encoding="utf-8")
    texts = next(e for e in json.loads(Path(argv[5]).read_text(encoding="utf-8"))["evals"] if e["id"] == eval_id)["expectations"]
    results = GRADERS[eval_id](repo, base, report)
    assert len(results) == len(texts), f"检查数 {len(results)} 与要求数 {len(texts)} 不一致"
    grading = {"expectations": [{"text": t, "passed": p, "evidence": e} for t, (p, e) in zip(texts, results)]}
    text = json.dumps(grading, ensure_ascii=False, indent=2)
    if "--output" in argv:
        Path(argv[argv.index("--output") + 1]).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
