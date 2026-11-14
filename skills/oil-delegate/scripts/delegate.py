#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

if sys.version_info < (3, 10):
    sys.stderr.write("[ERROR] oil-delegate requires Python 3.10 or newer.\n")
    sys.exit(1)

SCHEMA_VERSION = 1
SUPPORTED_RUNNERS = (
    "claude",
    "workbuddy",
    "antigravity",
    "codex",
    "opencode",
    "dsh",
    "grok",
    "kimi",
)
RUNNER_LABELS = {
    "claude": "Claude Code (claude/cc)",
    "workbuddy": "WorkBuddy (codebuddy/cbc/wb)",
    "antigravity": "Antigravity (agy/anti)",
    "codex": "Codex (codex)",
    "opencode": "OpenCode (opencode)",
    "dsh": "DeepSeek Harness (dsh)",
    "grok": "Grok Build (grok)",
    "kimi": "Kimi Code (kimi)",
}
RUNNER_BINARIES = {
    "claude": ("claude",),
    "workbuddy": ("codebuddy", "cbc"),
    "antigravity": ("agy",),
    "codex": ("codex",),
    "opencode": ("opencode",),
    "dsh": ("dsh",),
    "grok": ("grok",),
    "kimi": ("kimi",),
}
RUNNER_ALIASES = {
    "cc": "claude",
    "claude-code": "claude",
    "wb": "workbuddy",
    "codebuddy": "workbuddy",
    "cbc": "workbuddy",
    "agy": "antigravity",
    "anti": "antigravity",
    "deepseek": "dsh",
    "deepseek-harness": "dsh",
    "grok-build": "grok",
    "kimi-code": "kimi",
}
# Bump when the `run --json` result fields change incompatibly.
CONTRACT_VERSION = 1
# Interactive commands that reopen a headless session; {bin} is the detected executable name.
RESUME_TEMPLATES = {
    "claude": ("{bin}", "--resume", "{sid}"),
    "workbuddy": ("{bin}", "--resume", "{sid}"),
    "antigravity": ("{bin}", "--conversation", "{sid}"),
    "codex": ("{bin}", "resume", "{sid}"),
    "opencode": ("{bin}", "--session", "{sid}"),
    "dsh": ("{bin}", "tui", "--resume", "{sid}"),
    "grok": ("{bin}", "--resume", "{sid}"),
    "kimi": ("{bin}", "-S", "{sid}"),
}
EXIT_NEEDS_ONBOARDING = 10
EXIT_NEEDS_RUNNER_CHOICE = 11
EXIT_NEEDS_ROLE_CHOICE = 12
EXIT_USE_HOST = 13
# Roles let calling skills say what a task is for; the user's saved preference picks the agent.
# Review-like roles always run read-only, so callers never need to remember --mode read.
ROLE_MODES = {
    "review": "read",
    "critique": "read",
    "diagnose": "read",
    "implement": "write",
}
ROLE_DESCRIPTIONS = {
    "review": "independent code review",
    "critique": "independent critique of a plan before implementation",
    "diagnose": "two independent root-cause analyses of a stuck problem",
    "implement": "implementing an approved plan",
}
ROLE_SLOTS = {"diagnose": 2}
HOST_AGENT = "host"
READ_ONLY_UNSUPPORTED = {"kimi"}
DEFAULT_RUN_TIMEOUT = 3600


class DelegateError(Exception):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def default_config() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "onboarded": False,
        "enabled_runners": [],
        "default_runner": None,
        "runner_paths": {},
        "roles": {},
    }


def resolve_state_dir(explicit: str | None = None) -> Path:
    value = explicit or os.environ.get("OIL_DELEGATE_HOME")
    if value:
        return Path(value).expanduser().resolve()
    return (Path.home() / ".oil-delegate").resolve()


def load_config(state_dir: Path) -> dict[str, Any]:
    path = state_dir / "config.json"
    if not path.exists():
        return default_config()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DelegateError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise DelegateError(f"Config must be a JSON object: {path}")
    merged = {**default_config(), **loaded}
    if not isinstance(merged.get("enabled_runners"), list):
        raise DelegateError("config.enabled_runners must be a list")
    if not isinstance(merged.get("runner_paths"), dict):
        raise DelegateError("config.runner_paths must be an object")
    if not isinstance(merged.get("roles"), dict):
        raise DelegateError("config.roles must be an object")
    return merged


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)


def save_config(state_dir: Path, config: dict[str, Any]) -> None:
    atomic_write_json(state_dir / "config.json", config)


def canonical_runner(value: str) -> str:
    runner = value.strip().lower()
    runner = RUNNER_ALIASES.get(runner, runner)
    if runner not in SUPPORTED_RUNNERS:
        raise DelegateError(
            f"Unsupported runner '{value}'. Choose from: {', '.join(SUPPORTED_RUNNERS)}"
        )
    return runner


def parse_runner_list(raw: str) -> list[str]:
    result: list[str] = []
    for item in raw.replace(";", ",").replace(" ", ",").split(","):
        if not item.strip():
            continue
        runner = canonical_runner(item)
        if runner not in result:
            result.append(runner)
    return result


def is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def detect_binary(runner: str, config: dict[str, Any]) -> dict[str, Any]:
    env_key = f"OIL_DELEGATE_{runner.upper()}_BIN"
    configured = os.environ.get(env_key) or config.get("runner_paths", {}).get(runner)
    source = "environment" if os.environ.get(env_key) else "config"
    if configured:
        path = Path(configured).expanduser().resolve()
        return {
            "installed": is_executable(path),
            "path": str(path),
            "source": source,
            "error": None if is_executable(path) else f"{env_key} or configured path is not executable",
        }

    for binary in RUNNER_BINARIES[runner]:
        found = shutil.which(binary)
        if found and is_executable(Path(found)):
            return {"installed": True, "path": found, "source": "PATH", "error": None}

    home = Path.home()
    fallback_dirs = [
        home / ".local" / "bin",
        home / ".kimi-code" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ]
    for directory in fallback_dirs:
        for binary in RUNNER_BINARIES[runner]:
            candidate = directory / binary
            if is_executable(candidate):
                return {
                    "installed": True,
                    "path": str(candidate.resolve()),
                    "source": "known-path",
                    "error": None,
                }

    if runner == "workbuddy" and sys.platform == "darwin":
        candidate = Path(
            "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy"
        )
        if is_executable(candidate):
            return {
                "installed": True,
                "path": str(candidate),
                "source": "workbuddy-app",
                "error": None,
            }

    return {
        "installed": False,
        "path": None,
        "source": None,
        "error": f"Command not found: {' or '.join(RUNNER_BINARIES[runner])}",
    }


def probe_version(path: str) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"version": None, "probe_error": str(exc)}
    output = (proc.stdout or proc.stderr or "").strip()
    return {
        "version": output.splitlines()[0][:200] if output else None,
        "probe_error": None if proc.returncode == 0 else f"--version exited {proc.returncode}",
    }


def probe_dsh_capabilities(path: str) -> dict[str, bool | str | None]:
    try:
        proc = subprocess.run(
            [path, "--profile", "headless", "--help"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "json": False,
            "resume": False,
            "permission_flag": False,
            "approval_flag": False,
            "probe_error": str(exc),
        }
    text = f"{proc.stdout}\n{proc.stderr}"
    return {
        "json": "--json" in text,
        "resume": "--session-id" in text,
        "permission_flag": "--permission-mode" in text,
        "approval_flag": "--approval-policy" in text,
        "probe_error": None if proc.returncode == 0 else f"headless --help exited {proc.returncode}",
    }


def scan_runners(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    enabled = config.get("enabled_runners", [])
    default_runner = config.get("default_runner")
    result: dict[str, dict[str, Any]] = {}
    for runner in SUPPORTED_RUNNERS:
        info = {
            "runner": runner,
            "label": RUNNER_LABELS[runner],
            "aliases": sorted(
                alias for alias, target in RUNNER_ALIASES.items() if target == runner
            ),
            **detect_binary(runner, config),
            "enabled": runner in enabled,
            "is_default": runner == default_runner,
            "version": None,
            "probe_error": None,
        }
        if info["installed"]:
            info.update(probe_version(info["path"]))
            if runner == "dsh":
                info["capabilities"] = probe_dsh_capabilities(info["path"])
        result[runner] = info
    return result


def onboarding_payload(state_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    detected = scan_runners(config)
    onboarded = bool(config.get("onboarded") and config.get("enabled_runners"))
    return {
        "status": "ONBOARDED" if onboarded else "NEEDS_ONBOARDING",
        "onboarded": onboarded,
        "state_dir": str(state_dir),
        "enabled_runners": config.get("enabled_runners", []),
        "default_runner": config.get("default_runner"),
        "roles": config.get("roles", {}),
        "installed_runners": [name for name, info in detected.items() if info["installed"]],
        "detected_runners": detected,
        "next_step_hint": (
            "Onboarding complete; delegate with run."
            if onboarded
            else "Show detected runners, ask which to enable and whether to set a default, then rerun onboard with --enable and --default-runner."
        ),
    }


def validate_installed(runners: list[str], config: dict[str, Any]) -> None:
    missing = [runner for runner in runners if not detect_binary(runner, config)["installed"]]
    if missing:
        raise DelegateError(
            f"Cannot enable missing or non-executable runners: {', '.join(missing)}. "
            "Install them or configure an executable path first."
        )


def apply_enabled_runners(config: dict[str, Any], raw: str) -> None:
    enabled = parse_runner_list(raw)
    validate_installed(enabled, config)
    config["enabled_runners"] = enabled
    config["onboarded"] = bool(enabled)
    if config.get("default_runner") not in enabled:
        config["default_runner"] = None
    # A role that points at a runner the user just disabled falls back to asking again.
    config["roles"] = {
        role: choices
        for role, choices in config.get("roles", {}).items()
        if all(choice == HOST_AGENT or choice in enabled for choice in choices)
    }


def apply_default_runner(config: dict[str, Any], raw: str) -> None:
    value = raw.strip().lower()
    if value in ("ask", "none", "null", ""):
        config["default_runner"] = None
        return
    runner = canonical_runner(value)
    validate_installed([runner], config)
    enabled = list(config.get("enabled_runners", []))
    if runner not in enabled:
        enabled.append(runner)
    config["enabled_runners"] = enabled
    config["default_runner"] = runner
    config["onboarded"] = True


def canonical_role(value: str) -> str:
    role = value.strip().lower()
    if role not in ROLE_MODES:
        raise DelegateError(f"Unknown role '{value}'. Choose from: {', '.join(ROLE_MODES)}")
    return role


def apply_role(config: dict[str, Any], assignment: str) -> list[str]:
    """Save ROLE=<runner|host|ask>; diagnose takes two comma-separated choices. Returns warnings."""
    if "=" not in assignment:
        raise DelegateError("--set-role expects ROLE=<runner|host|ask>, e.g. review=codex")
    role_raw, value_raw = assignment.split("=", 1)
    role = canonical_role(role_raw)
    roles = dict(config.get("roles", {}))
    value = value_raw.strip().lower()
    if value in ("ask", "none", "null", ""):
        roles.pop(role, None)
        config["roles"] = roles
        return []

    slots = ROLE_SLOTS.get(role, 1)
    choices: list[str] = []
    for item in value.replace(";", ",").split(","):
        if item.strip():
            choices.append(HOST_AGENT if item.strip() == HOST_AGENT else canonical_runner(item))
    if len(choices) != slots or len(set(choices)) != slots:
        need = f"{slots} different choices" if slots > 1 else "exactly one choice"
        raise DelegateError(f"Role '{role}' needs {need}; got: {value_raw.strip()}")
    runners = [choice for choice in choices if choice != HOST_AGENT]
    if ROLE_MODES[role] == "read":
        blocked = [runner for runner in runners if runner in READ_ONLY_UNSUPPORTED]
        if blocked:
            raise DelegateError(
                f"Role '{role}' runs read-only, which {', '.join(blocked)} cannot guarantee; choose another runner or host"
            )
    validate_installed(runners, config)
    if runners:
        enabled = list(config.get("enabled_runners", []))
        enabled.extend(runner for runner in runners if runner not in enabled)
        config["enabled_runners"] = enabled
        config["onboarded"] = True
    roles[role] = choices
    config["roles"] = roles
    return role_warnings(roles)


def role_warnings(roles: dict[str, list[str]]) -> list[str]:
    warnings: list[str] = []
    implementer = (roles.get("implement") or [None])[0]
    if implementer and implementer != HOST_AGENT:
        for role in ("review", "critique"):
            if (roles.get(role) or [None])[0] == implementer:
                warnings.append(
                    f"'{role}' uses the same runner as 'implement' ({implementer}); "
                    "an independent review works best with a different agent"
                )
    return warnings


def resolve_role(args: argparse.Namespace, config: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
    """Fill args.runner and args.mode from a role. Returns (exit_code, payload) when the caller must act first."""
    role = canonical_role(args.role)
    mode = ROLE_MODES[role]
    if args.mode and args.mode != mode:
        raise DelegateError(f"Role '{role}' always runs in {mode} mode; drop --mode {args.mode}")
    args.mode = mode
    slots = ROLE_SLOTS.get(role, 1)
    if not 1 <= args.role_slot <= slots:
        raise DelegateError(f"--role-slot for '{role}' must be between 1 and {slots}")
    args.resolved_role = role

    if not args.runner:
        choices = config.get("roles", {}).get(role) or []
        if len(choices) != slots:
            onboarded = bool(config.get("onboarded") and config.get("enabled_runners"))
            pool = (
                config.get("enabled_runners", [])
                if onboarded
                else [r for r in SUPPORTED_RUNNERS if detect_binary(r, config)["installed"]]
            )
            candidates = [HOST_AGENT] + [
                r for r in pool if not (mode == "read" and r in READ_ONLY_UNSUPPORTED)
            ]
            return EXIT_NEEDS_ROLE_CHOICE, {
                "status": "NEEDS_ROLE_CHOICE",
                "role": role,
                "role_description": ROLE_DESCRIPTIONS[role],
                "mode": mode,
                "slots": slots,
                "candidates": candidates,
                "message": (
                    "Ask the user once which agent should handle this role "
                    f"({HOST_AGENT} = the current agent's own fresh subagent). Save it with "
                    f"config --set-role {role}=<choice>, or pass --runner for this run only."
                ),
            }
        choice = choices[args.role_slot - 1]
        if choice == HOST_AGENT:
            return EXIT_USE_HOST, {
                "status": "USE_HOST",
                "role": role,
                "role_description": ROLE_DESCRIPTIONS[role],
                "mode": mode,
                "role_slot": args.role_slot,
                "message": (
                    "The user prefers the host agent for this role: run it in a fresh subagent "
                    f"of the current agent ({mode} only) instead of an external CLI."
                ),
            }
        args.runner = choice

    if mode == "read" and canonical_runner(args.runner) in READ_ONLY_UNSUPPORTED:
        raise DelegateError(f"Role '{role}' runs read-only, which {args.runner} cannot guarantee")
    return None


def config_view_payload(state_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    onboarded = bool(config.get("onboarded") and config.get("enabled_runners"))
    return {
        "status": "ONBOARDED" if onboarded else "NEEDS_ONBOARDING",
        "onboarded": onboarded,
        "state_dir": str(state_dir),
        "enabled_runners": config.get("enabled_runners", []),
        "default_runner": config.get("default_runner"),
        "runner_paths": config.get("runner_paths", {}),
        "roles": config.get("roles", {}),
        "role_names": list(ROLE_MODES),
    }


def cmd_onboard(args: argparse.Namespace, state_dir: Path) -> int:
    config = load_config(state_dir)
    changed = args.enable is not None or args.default_runner is not None
    if args.scan and changed:
        raise DelegateError("--scan is read-only and cannot be combined with configuration changes")
    if args.enable is not None:
        apply_enabled_runners(config, args.enable)
    if args.default_runner is not None:
        apply_default_runner(config, args.default_runner)
    if changed:
        save_config(state_dir, config)
    print(json.dumps(onboarding_payload(state_dir, config), ensure_ascii=False, indent=2))
    return 0


def parse_bin_assignment(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise DelegateError("--set-bin expects RUNNER=/path/to/executable")
    runner_raw, path = value.split("=", 1)
    runner = canonical_runner(runner_raw)
    if not path.strip():
        raise DelegateError("--set-bin requires a non-empty path")
    return runner, path.strip()


def cmd_config(args: argparse.Namespace, state_dir: Path) -> int:
    config = load_config(state_dir)
    changed = any(
        (
            args.set_enabled_runners is not None,
            args.set_default_runner is not None,
            bool(args.set_bin),
            bool(args.clear_bin),
            bool(args.set_role),
        )
    )

    for assignment in args.set_bin or []:
        runner, raw_path = parse_bin_assignment(assignment)
        path = Path(raw_path).expanduser().resolve()
        if not is_executable(path):
            raise DelegateError(f"Not executable: {path}")
        paths = dict(config.get("runner_paths", {}))
        paths[runner] = str(path)
        config["runner_paths"] = paths

    for runner_raw in args.clear_bin or []:
        runner = canonical_runner(runner_raw)
        paths = dict(config.get("runner_paths", {}))
        if runner in paths:
            del paths[runner]
            config["runner_paths"] = paths

    if args.set_enabled_runners is not None:
        apply_enabled_runners(config, args.set_enabled_runners)
    if args.set_default_runner is not None:
        apply_default_runner(config, args.set_default_runner)
    warnings: list[str] = []
    for assignment in args.set_role or []:
        warnings = apply_role(config, assignment)

    if changed:
        save_config(state_dir, config)
        payload = onboarding_payload(state_dir, config)
    else:
        payload = config_view_payload(state_dir, config)
    if warnings:
        payload["warnings"] = warnings
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def resolve_workspace(raw: str) -> Path:
    workspace = Path(raw).expanduser().resolve()
    if not workspace.is_dir():
        raise DelegateError(f"Workspace is not a directory: {workspace}")
    return workspace


def compose_prompt(prompt: str, workspace: Path, mode: str, files: list[str]) -> str:
    resolved_files: list[str] = []
    for raw in files:
        path = Path(raw).expanduser()
        resolved = path.resolve() if path.is_absolute() else (workspace / path).resolve()
        if not resolved.is_file():
            raise DelegateError(f"Entry file does not exist: {resolved}")
        resolved_files.append(str(resolved))
    lines = [
        prompt.strip(),
        "",
        "[oil-delegate runtime context]",
        f"Workspace: {workspace}",
        f"Mode: {mode}",
    ]
    if resolved_files:
        lines.append("Entry files to inspect first: " + ", ".join(resolved_files))
    if mode == "read":
        lines.append("Read only: do not create, edit, delete, or otherwise modify files; avoid side-effecting commands.")
    else:
        lines.append("Keep all changes inside the workspace and within the requested scope.")
    lines.extend(
        [
            "Final report: summarize changes or findings, list files involved, give exact verification commands and results, and identify anything blocked or unverified.",
        ]
    )
    return "\n".join(lines)


def build_invocation(
    runner: str,
    binary: str,
    workspace: Path,
    prompt: str,
    session_id: str | None,
    mode: str,
    dsh_capabilities: dict[str, Any] | None,
) -> tuple[list[str], str | None, dict[str, str]]:
    env: dict[str, str] = {}
    stdin_input: str | None = None

    if runner == "claude":
        cmd = [binary]
        if session_id:
            cmd.extend(["--resume", session_id])
        cmd.extend(["-p", prompt, "--output-format", "json"])
        cmd.extend(
            ["--permission-mode", "plan", "--permission-prompts", "none"]
            if mode == "read"
            else ["--dangerously-skip-permissions"]
        )
        return cmd, stdin_input, env

    if runner == "workbuddy":
        cmd = [binary]
        if session_id:
            cmd.extend(["--resume", session_id])
        cmd.extend(["-p", prompt, "--output-format", "json"])
        cmd.extend(
            ["--permission-mode", "plan", "-y"]
            if mode == "read"
            else ["--permission-mode", "bypassPermissions", "-y"]
        )
        env["CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
        if mode == "write":
            env["CODEBUDDY_IS_SANDBOX"] = "1"
        return cmd, stdin_input, env

    if runner == "antigravity":
        cmd = [binary]
        if session_id:
            cmd.extend(["--conversation", session_id])
        cmd.extend(["-p", prompt, "--output-format", "json"])
        cmd.extend(["--mode", "plan"] if mode == "read" else ["--dangerously-skip-permissions"])
        return cmd, stdin_input, env

    if runner == "codex":
        if mode == "read":
            permission = ["-c", 'sandbox_mode="read-only"'] if session_id else ["--sandbox", "read-only"]
        else:
            permission = ["--dangerously-bypass-approvals-and-sandbox"]
        if session_id:
            cmd = [binary, "exec", "resume", "--json", "--skip-git-repo-check", *permission, session_id, "-"]
        else:
            cmd = [binary, "exec", "--cd", str(workspace), "--json", "--skip-git-repo-check", *permission, "-"]
        return cmd, prompt, env

    if runner == "grok":
        cmd = [binary, "--cwd", str(workspace)]
        if session_id:
            cmd.extend(["--resume", session_id])
        cmd.extend(["-p", prompt, "--output-format", "json"])
        cmd.extend(["--permission-mode", "plan"] if mode == "read" else ["--always-approve"])
        return cmd, stdin_input, env

    if runner == "kimi":
        if mode == "read":
            raise DelegateError(
                "Kimi Code non-interactive mode uses auto permissions and cannot guarantee read-only"
            )
        cmd = [binary]
        if session_id:
            cmd.extend(["-S", session_id])
        cmd.extend(["-p", prompt, "--output-format", "stream-json"])
        return cmd, stdin_input, env

    if runner == "opencode":
        cmd = [binary, "run", "--format", "json", "--dir", str(workspace)]
        if session_id:
            cmd.extend(["--session", session_id])
        if mode == "read":
            cmd.extend(["--agent", "plan"])
        cmd.append(prompt)
        permission = {
            "*": "allow",
            "edit": "deny" if mode == "read" else "allow",
            "bash": "deny" if mode == "read" else "allow",
            "external_directory": "deny" if mode == "read" else "allow",
            "question": "deny",
        }
        env["OPENCODE_PERMISSION"] = json.dumps(permission, separators=(",", ":"))
        return cmd, stdin_input, env

    if runner == "dsh":
        capabilities = dsh_capabilities or {}
        if not capabilities.get("json"):
            raise DelegateError("This DSH headless installation lacks --json; upgrade DSH to use oil-delegate")
        if session_id and not capabilities.get("resume"):
            raise DelegateError("This DSH headless installation lacks --session-id; upgrade DSH to resume sessions")
        cmd = [binary, "--profile", "headless"]
        permission = "read-only" if mode == "read" else "danger-full-access"
        if capabilities.get("permission_flag"):
            cmd.extend(["--permission-mode", permission])
        else:
            env["DSH_PERMISSION_MODE"] = permission
        if capabilities.get("approval_flag"):
            cmd.extend(["--approval-policy", "never"])
        if session_id:
            cmd.extend(["--session-id", session_id])
        cmd.extend(["--json", "-"])
        return cmd, prompt, env

    raise DelegateError(f"Unsupported runner: {runner}")


def resume_command(runner: str, binary: str, session_id: str | None) -> str | None:
    if not session_id:
        return None
    # Standard executable names stay short; custom paths are kept in full so the command still works.
    name = Path(binary).name if Path(binary).name in RUNNER_BINARIES[runner] else binary
    return shlex.join(part.format(bin=name, sid=session_id) for part in RESUME_TEMPLATES[runner])


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps({"contract_version": CONTRACT_VERSION, **payload}, ensure_ascii=False, indent=2))


def redact_command(command: list[str], prompt: str) -> list[str]:
    return ["<prompt>" if part == prompt else part for part in command]


def json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    for line in reversed(stripped.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def json_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def nested_session_id(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("sessionID", "sessionId", "session_id", "conversation_id", "thread_id"):
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
    for key in ("part", "session", "final", "result", "init"):
        nested = nested_session_id(value.get(key))
        if nested:
            return nested
    return None


def error_message(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("message", "error", "reason", "content"):
            found = error_message(value.get(key))
            if found:
                return found
        for key in ("data", "error"):
            found = error_message(value.get(key))
            if found:
                return found
    return str(value)


def parse_runner_output(
    runner: str,
    raw_out: str,
    raw_err: str,
    exit_code: int,
    previous_session: str | None,
) -> dict[str, Any]:
    session_id = previous_session
    response = ""
    error: str | None = None
    protocol_ok = True

    if runner in ("claude", "workbuddy", "antigravity"):
        obj = json_object(raw_out)
        if not obj:
            protocol_ok = False
            error = "CLI did not return a JSON result"
        elif runner == "antigravity":
            session_id = obj.get("conversation_id") or session_id
            response = str(obj.get("response") or "")
            if obj.get("status") != "SUCCESS":
                error = error_message(obj.get("error") or obj.get("status"))
        else:
            session_id = obj.get("session_id") or session_id
            response = str(obj.get("result") or "")
            if obj.get("is_error"):
                error = response or "CLI reported an error"

    elif runner == "grok":
        obj = json_object(raw_out)
        if not obj:
            protocol_ok = False
            error = "CLI did not return a JSON result"
        else:
            session_id = obj.get("sessionId") or nested_session_id(obj) or session_id
            response = str(obj.get("text") or "")
            if obj.get("type") == "error":
                error = error_message(obj.get("message") or obj)
            elif obj.get("stopReason") in ("cancelled", "refusal", "error", "error_during_execution"):
                error = error_message(obj.get("message") or obj.get("stopReason"))
            protocol_ok = bool(session_id and (response or error))

    elif runner == "kimi":
        chunks: list[str] = []
        for event in json_events(raw_out):
            session_id = nested_session_id(event) or session_id
            if event.get("type") == "session.resume_hint" and event.get("session_id"):
                session_id = str(event["session_id"])
            if event.get("role") == "assistant" and event.get("content"):
                content = event["content"]
                if isinstance(content, str):
                    chunks.append(content)
                elif isinstance(content, list):
                    chunks.extend(
                        str(block["text"])
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
                    )
            elif event.get("type") == "error" or event.get("role") == "error":
                error = error_message(event.get("content") or event.get("message") or event)
        response = "\n".join(chunks).strip()
        protocol_ok = bool(session_id and (chunks or error))

    elif runner == "codex":
        messages: list[str] = []
        for event in json_events(raw_out):
            session_id = nested_session_id(event) or session_id
            event_type = event.get("type")
            if event_type == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    messages.append(str(item["text"]))
            elif event_type in ("turn.failed", "error"):
                error = error_message(event.get("error") or event.get("message")) or "Codex turn failed"
        response = messages[-1] if messages else ""
        protocol_ok = bool(session_id and (messages or error))

    elif runner == "opencode":
        chunks: list[str] = []
        for event in json_events(raw_out):
            session_id = nested_session_id(event) or session_id
            event_type = event.get("type")
            part = event.get("part") if isinstance(event.get("part"), dict) else {}
            if event_type == "text" and part.get("text"):
                chunks.append(str(part["text"]))
            elif event_type == "error":
                error = error_message(event.get("error") or event.get("message") or event)
        response = "".join(chunks)
        protocol_ok = bool(session_id and (chunks or error))

    elif runner == "dsh":
        chunks: list[str] = []
        final: dict[str, Any] | None = None
        turn_reason: Any = None
        for event in json_events(raw_out):
            session_id = nested_session_id(event) or session_id
            event_type = event.get("type")
            if event_type == "text" and event.get("text"):
                chunks.append(str(event["text"]))
            elif event_type == "status" and event.get("phase") == "turn_end":
                turn_reason = event.get("reason")
            elif event_type == "final":
                final = event
            elif event_type == "error":
                error = error_message(event.get("message") or event.get("error") or event)
        if final:
            response = str(final.get("text") or "")
            reason = final.get("reason") or turn_reason
            if reason is not None and (
                not isinstance(reason, dict) or reason.get("kind") != "completed"
            ):
                error = error_message(reason) or "DSH final reason is not completed"
            elif reason is None and exit_code != 0 and not error:
                error = "DSH run did not complete"
        else:
            response = "".join(chunks)
            if not error:
                error = "DSH stream ended without a final event"
        protocol_ok = bool(session_id and final and not error)

    status = (
        "completed"
        if exit_code == 0 and protocol_ok and not error and session_id and response.strip()
        else "failed"
    )
    if status == "failed":
        if not error:
            if not protocol_ok:
                error = "Runner returned an invalid or incomplete response"
            elif not session_id:
                error = "Runner did not return a session ID"
            elif not response.strip():
                error = "Runner returned an empty response"
            elif exit_code != 0:
                error = f"Runner exited with code {exit_code}"
            else:
                error = "Runner failed"
        stderr_detail = raw_err.strip()[-2000:]
        if stderr_detail and (not error or error not in stderr_detail):
            error = f"{error}\n{stderr_detail}" if error else stderr_detail
    return {
        "status": status,
        "session_id": session_id,
        "response": response,
        "error": error,
    }


def terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=3)
        return
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass


def execute_run(
    args: argparse.Namespace,
    state_dir: Path,
    config: dict[str, Any],
    *,
    require_enabled: bool = True,
) -> int:
    runner = canonical_runner(args.runner)
    if require_enabled and runner not in config.get("enabled_runners", []):
        raise DelegateError(f"Runner '{runner}' is not enabled; enable it with onboard or config")
    workspace = resolve_workspace(args.workspace)
    if args.timeout <= 0:
        raise DelegateError("--timeout must be greater than zero")
    prompt = compose_prompt(args.prompt, workspace, args.mode, args.file or [])
    binary_info = detect_binary(runner, config)
    if not binary_info["installed"]:
        raise DelegateError(binary_info["error"] or f"Runner not installed: {runner}")
    capabilities = probe_dsh_capabilities(binary_info["path"]) if runner == "dsh" else None
    command, stdin_input, env_updates = build_invocation(
        runner,
        binary_info["path"],
        workspace,
        prompt,
        args.session,
        args.mode,
        capabilities,
    )
    safe_command = redact_command(command, prompt)

    if args.dry_run:
        payload = {
            "status": "DRY_RUN",
            "runner": runner,
            "role": getattr(args, "resolved_role", None),
            "workspace": str(workspace),
            "mode": args.mode,
            "session_id": args.session,
            "command": safe_command,
            "stdin": stdin_input is not None,
            "environment_overrides": env_updates,
        }
        emit(payload)
        return 0

    run_id = f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:10]}"
    run_dir = state_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    paths = {
        "prompt": run_dir / "prompt.txt",
        "stderr": run_dir / "stderr.log",
        "events": run_dir / "events.jsonl",
        "output": run_dir / "output.md",
        "result": run_dir / "result.json",
    }
    paths["prompt"].write_text(prompt, encoding="utf-8")
    started_at = now_iso()
    started = time.monotonic()
    raw_out = ""
    raw_err = ""
    exit_code = 1
    env = os.environ.copy()
    env.update(env_updates)

    popen_args: dict[str, Any] = {}
    if os.name == "posix":
        popen_args["start_new_session"] = True
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        popen_args["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        proc = subprocess.Popen(
            command,
            cwd=str(workspace),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            **popen_args,
        )
        try:
            raw_out, raw_err = proc.communicate(input=stdin_input, timeout=args.timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            terminate_process(proc)
            raw_out, raw_err = proc.communicate()
            exit_code = 124
            raw_err = f"{raw_err}\nTimeout expired after {args.timeout}s".strip()
    except OSError as exc:
        raw_err = str(exc)
        exit_code = 127

    raw_out = raw_out or ""
    raw_err = raw_err or ""
    paths["stderr"].write_text(raw_err, encoding="utf-8")
    paths["events"].write_text(raw_out, encoding="utf-8")
    parsed = parse_runner_output(runner, raw_out, raw_err, exit_code, args.session)
    elapsed = time.monotonic() - started
    finished_at = now_iso()

    markdown = f"## Summary ({runner})\n\n{parsed['response']}\n"
    if parsed["error"]:
        markdown += f"\n## Error\n\n```text\n{parsed['error']}\n```\n"
    paths["output"].write_text(markdown, encoding="utf-8")
    result = {
        **parsed,
        "exit_code": exit_code,
        "runner": runner,
        "role": getattr(args, "resolved_role", None),
        "workspace": str(workspace),
        "mode": args.mode,
        "resume_command": resume_command(runner, binary_info["path"], parsed["session_id"]),
        "command": safe_command,
        "environment_overrides": env_updates,
        "output_path": str(paths["output"]),
        "result_path": str(paths["result"]),
        "events_path": str(paths["events"]),
        "raw_stdout_path": str(paths["events"]),
        "stderr_path": str(paths["stderr"]),
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_seconds": round(elapsed, 3),
    }
    atomic_write_json(paths["result"], result)

    if parsed["status"] == "completed":
        return_code = 0
    else:
        return_code = exit_code if 0 < exit_code < 256 else 1
    if args.json:
        emit(result)
        return return_code
    print(f"status={parsed['status']}")
    print(f"session_id={parsed['session_id'] or ''}")
    print(f"runner={runner}")
    print(f"output_path={paths['output']}")
    print(f"result_path={paths['result']}")
    print(f"events_path={paths['events']}")
    print(f"elapsed={elapsed:.1f}s")
    return return_code


def cmd_run(args: argparse.Namespace, state_dir: Path) -> int:
    config = load_config(state_dir)
    if args.role:
        pending = resolve_role(args, config)
        if pending:
            exit_code, payload = pending
            emit(payload)
            return exit_code
    args.mode = args.mode or "write"
    if not (config.get("onboarded") and config.get("enabled_runners")):
        if args.runner:
            return execute_run(args, state_dir, config, require_enabled=False)
        payload = onboarding_payload(state_dir, config)
        payload["how_to_onboard"] = (
            "delegate.py onboard --enable <runners> --default-runner <runner|ask>"
        )
        emit(payload)
        return EXIT_NEEDS_ONBOARDING

    if not args.runner and not config.get("default_runner"):
        payload = {
            "status": "NEEDS_RUNNER_CHOICE",
            "enabled_runners": config.get("enabled_runners", []),
            "available_runners": {
                runner: detect_binary(runner, config)
                for runner in config.get("enabled_runners", [])
            },
            "message": "Choose a runner for this task or configure a default runner.",
        }
        emit(payload)
        return EXIT_NEEDS_RUNNER_CHOICE

    if not args.runner:
        args.runner = config["default_runner"]
    return execute_run(args, state_dir, config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Delegate one task to an external CLI agent")
    parser.add_argument("--home", help="Override the oil-delegate state directory")
    sub = parser.add_subparsers(dest="command", required=True)

    onboard = sub.add_parser("onboard", help="Scan and configure external CLI runners")
    onboard.add_argument("--scan", action="store_true", help="Read-only environment scan")
    onboard.add_argument("--enable", help="Comma-separated runner names or aliases")
    onboard.add_argument("--default-runner", help="Default runner, or ask")

    config = sub.add_parser("config", help="Read or update runner configuration")
    config.add_argument("--set-enabled-runners")
    config.add_argument("--set-default-runner")
    config.add_argument("--set-bin", action="append", help="RUNNER=/path/to/executable")
    config.add_argument("--clear-bin", action="append", help="Runner whose configured path should be cleared")
    config.add_argument(
        "--set-role",
        action="append",
        help="ROLE=<runner|host|ask>; roles: review, critique, diagnose (two choices), implement",
    )

    run = sub.add_parser("run", help="Run or resume one delegated task")
    run.add_argument("--prompt", required=True)
    run.add_argument("--runner")
    run.add_argument("--workspace", default=os.getcwd())
    run.add_argument("--mode", choices=("read", "write"), help="Defaults to write, or to the role's mode")
    run.add_argument("--role", help="review, critique, diagnose or implement; picks the saved agent and mode")
    run.add_argument("--role-slot", type=int, default=1, help="Which saved choice to use for diagnose (1 or 2)")
    run.add_argument("--file", action="append")
    run.add_argument("--session")
    run.add_argument("--timeout", type=int, default=DEFAULT_RUN_TIMEOUT)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument(
        "--json",
        action="store_true",
        help="Print one JSON object (result, status payload or error) instead of key=value lines",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    state_dir = resolve_state_dir(args.home)
    try:
        if args.command == "onboard":
            return cmd_onboard(args, state_dir)
        if args.command == "config":
            return cmd_config(args, state_dir)
        if args.command == "run":
            return cmd_run(args, state_dir)
        raise DelegateError(f"Unknown command: {args.command}")
    except DelegateError as exc:
        if getattr(args, "json", False):
            emit({"status": "ERROR", "error": str(exc)})
        else:
            sys.stderr.write(f"[ERROR] {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
