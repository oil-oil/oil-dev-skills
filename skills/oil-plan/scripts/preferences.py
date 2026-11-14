#!/usr/bin/env python3
"""读写 oil-dev 系列 Skill 的个人偏好文件（默认 ~/.oil-dev/preferences.md，仅使用 Python 3.10+ 标准库）。

整个系列共用一个 Markdown 文件，用自然语言写：

    ## oil-explore              ← 每个 Skill 一节
    这里的文字对所有宿主生效。
    ### antigravity             ← 某个宿主专属的写法
    - 用 swarm 派发探索子 Agent ……

项目级、团队共享的约定写在仓库 AGENTS.md 的 oil-dev 小节里，不放这里；
oil-delegate 的偏好保存在它自己的配置里，也不放这里。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

if sys.version_info < (3, 10):
    sys.stderr.write("错误：需要 Python 3.10 或更高版本。\n")
    raise SystemExit(2)

CONTRACT_VERSION = 1
HOME_ENV = "OIL_DEV_HOME"
FILE_NAME = "preferences.md"
MAX_TEXT_LENGTH = 4000
KNOWN_SKILLS = ("oil-explore", "oil-plan", "oil-code-review", "oil-govern")
NEW_FILE_HEADER = (
    "# oil-dev 个人偏好\n\n"
    "每个 Skill 一节（## Skill 名）。紧跟在 Skill 标题下的文字对所有宿主生效，"
    "### 宿主名 下面的文字只对该宿主生效。\n"
)

HOST_ALIASES = {
    "cc": "claude-code",
    "claude": "claude-code",
    "agy": "antigravity",
    "wb": "workbuddy",
    "codebuddy": "workbuddy",
    "dsh": "deepseek-harness",
    "deepseek": "deepseek-harness",
    "grok": "grok-build",
    "kimi": "kimi-code",
    "gemini": "gemini-cli",
}
HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


class PreferenceError(Exception):
    """可直接展示给用户的参数或文件错误。"""


def preferences_path(home: str | None = None) -> Path:
    base = home or os.environ.get(HOME_ENV) or str(Path.home() / ".oil-dev")
    return Path(base).expanduser() / FILE_NAME


def canonical_host(raw: str) -> str:
    host = re.sub(r"[\s_]+", "-", raw.strip().lower())
    host = HOST_ALIASES.get(host, host)
    if not HOST_RE.match(host):
        raise PreferenceError(f"宿主名称无效：{raw!r}。请用小写字母、数字和连字符，例如 claude-code、codex、antigravity")
    return host


def _check_skill(skill: str) -> str:
    if skill not in KNOWN_SKILLS:
        raise PreferenceError(f"没有名为 {skill!r} 的偏好分节，可用：{'、'.join(KNOWN_SKILLS)}")
    return skill


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """返回代码块之外的标题：(行号, 级别, 标题文字)。"""
    found, in_fence = [], False
    for index, line in enumerate(lines):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        match = None if in_fence else HEADING_RE.match(line)
        if match:
            found.append((index, len(match.group(1)), match.group(2)))
    return found


def _title_key(title: str) -> str:
    """取标题开头的名称部分，允许后面跟括号说明，如“Claude Code（Explore 子 Agent）”。"""
    return re.split(r"[^A-Za-z0-9 _-]", title.replace("`", "").strip(), maxsplit=1)[0].strip()


def _same_host(title: str, host: str) -> bool:
    try:
        return canonical_host(_title_key(title)) == host
    except PreferenceError:
        return False


def _locate(lines: list[str], skill: str) -> dict[str, Any] | None:
    """找到 Skill 小节，返回小节范围、共享文字范围与各宿主子节范围。"""
    heads = _headings(lines)
    for pos, (index, level, title) in enumerate(heads):
        if level != 2 or _title_key(title).lower() != skill:
            continue
        end = next((i for i, lv, _ in heads[pos + 1 :] if lv <= 2), len(lines))
        subs = [(i, lv, t) for i, lv, t in heads[pos + 1 :] if index < i < end]
        hosts, shared_end = [], end
        for sub_pos, (sub_index, sub_level, sub_title) in enumerate(subs):
            if sub_level != 3:
                continue
            shared_end = min(shared_end, sub_index)
            sub_end = next((i for i, lv, _ in subs[sub_pos + 1 :] if lv <= 3), end)
            hosts.append({"title": sub_title, "start": sub_index, "end": sub_end})
        return {"start": index, "end": end, "shared": (index + 1, shared_end), "hosts": hosts}
    return None


def _text(lines: list[str], start: int, end: int) -> str:
    return "\n".join(lines[start:end]).strip()


def load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise PreferenceError(f"无法读取偏好文件 {path}：{exc}") from exc


def save_lines(path: Path, lines: list[str]) -> None:
    cleaned: list[str] = []
    for line in lines:
        if line.strip() or (cleaned and cleaned[-1].strip()):
            cleaned.append(line.rstrip())
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".preferences-", suffix=".md", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(cleaned) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def get_preferences(path: Path, skill: str, host: str | None) -> dict[str, Any]:
    _check_skill(skill)
    host = canonical_host(host) if host else None
    lines = load_lines(path)
    section = _locate(lines, skill)
    shared = host_text = ""
    if section:
        shared = _text(lines, *section["shared"])
        match = next((h for h in section["hosts"] if host and _same_host(h["title"], host)), None)
        if match:
            host_text = _text(lines, match["start"] + 1, match["end"])
    return {
        "skill": skill,
        "host": host,
        "configured": bool(shared or host_text),
        "shared": shared,
        "host_text": host_text,
        "path": str(path),
    }


def _clean_text(text: str) -> list[str]:
    body = text.strip()
    if not body:
        raise PreferenceError("偏好内容为空；要删除请用 unset")
    if len(body) > MAX_TEXT_LENGTH:
        raise PreferenceError(f"偏好内容超过 {MAX_TEXT_LENGTH} 个字符，请精简后再保存")
    if any(HEADING_RE.match(line) for line in body.splitlines()):
        raise PreferenceError("偏好内容里不要写 Markdown 标题（# 开头的行），用列表或段落即可")
    return body.splitlines()


def set_preferences(path: Path, skill: str, host: str | None, text: str) -> dict[str, Any]:
    """整段替换：host 为 None 时替换对所有宿主生效的文字，否则替换该宿主的子节。"""
    _check_skill(skill)
    canon = canonical_host(host) if host else None
    body = _clean_text(text)
    lines = load_lines(path)
    if not lines:
        lines = NEW_FILE_HEADER.splitlines()
    section = _locate(lines, skill)
    if section is None:
        block = ["", f"## {skill}", ""]
        block += body if canon is None else ["", f"### {canon}", "", *body]
        lines = lines + block
    elif canon is None:
        start, end = section["shared"]
        lines = lines[:start] + ["", *body, ""] + lines[end:]
    else:
        match = next((h for h in section["hosts"] if _same_host(h["title"], canon)), None)
        if match:
            lines = lines[: match["start"] + 1] + ["", *body, ""] + lines[match["end"] :]
        else:
            end = section["end"]
            lines = lines[:end] + ["", f"### {canon}", "", *body, ""] + lines[end:]
    save_lines(path, lines)
    return get_preferences(path, skill, canon)


def unset_preferences(path: Path, skill: str, host: str | None) -> dict[str, Any]:
    _check_skill(skill)
    canon = canonical_host(host) if host else None
    lines = load_lines(path)
    section = _locate(lines, skill)
    if section is not None:
        if canon is None:
            start, end = section["shared"]
            lines = lines[:start] + [""] + lines[end:]
        else:
            match = next((h for h in section["hosts"] if _same_host(h["title"], canon)), None)
            if match:
                lines = lines[: match["start"]] + lines[match["end"] :]
        remaining = _locate(lines, skill)
        if remaining and not remaining["hosts"] and not _text(lines, *remaining["shared"]):
            lines = lines[: remaining["start"]] + lines[remaining["end"] :]
        save_lines(path, lines)
    return {"skill": skill, "host": canon, "removed": section is not None, "path": str(path)}


def show_preferences(path: Path) -> dict[str, Any]:
    lines = load_lines(path)
    return {"path": str(path), "exists": path.exists(), "content": "\n".join(lines), "skills": list(KNOWN_SKILLS)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="读写 oil-dev 系列 Skill 的个人偏好（默认 ~/.oil-dev/preferences.md）")
    parser.add_argument("--home", help=f"偏好目录，默认读取环境变量 {HOME_ENV}，否则为 ~/.oil-dev")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("show", help="显示偏好文件路径和全部内容")

    get = sub.add_parser("get", help="读取某个 Skill 对所有宿主生效的文字，以及当前宿主专属的文字")
    get.add_argument("skill")
    get.add_argument("--host", required=True, help="当前宿主，例如 claude-code、codex、antigravity")

    for name, help_text in (("set", "整段替换偏好文字"), ("unset", "删除偏好文字")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("skill")
        target = cmd.add_mutually_exclusive_group(required=True)
        target.add_argument("--host", help="只改这个宿主专属的部分")
        target.add_argument("--all-hosts", action="store_true", help="改对所有宿主生效的部分")
        if name == "set":
            cmd.add_argument("--text", required=True, help="完整的偏好文字；传 - 表示从标准输入读取")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = preferences_path(args.home)
    try:
        if args.command == "show":
            payload = show_preferences(path)
        elif args.command == "get":
            payload = get_preferences(path, args.skill, args.host)
        elif args.command == "set":
            text = sys.stdin.read() if args.text == "-" else args.text
            payload = set_preferences(path, args.skill, None if args.all_hosts else args.host, text)
        else:
            payload = unset_preferences(path, args.skill, None if args.all_hosts else args.host)
    except PreferenceError as exc:
        print(json.dumps({"contract_version": CONTRACT_VERSION, "status": "ERROR", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"contract_version": CONTRACT_VERSION, "status": "OK", **payload}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
