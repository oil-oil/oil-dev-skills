from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "preferences.py"
SPEC = importlib.util.spec_from_file_location("preferences", SCRIPT_PATH)
assert SPEC and SPEC.loader
preferences = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preferences)


def run_cli(home: Path | None, *args: str, env_home: Path | None = None, stdin: str | None = None) -> tuple[int, dict]:
    env = {k: v for k, v in os.environ.items() if k != preferences.HOME_ENV}
    cmd = [sys.executable, str(SCRIPT_PATH)]
    if home is not None:
        cmd += ["--home", str(home)]
    if env_home is not None:
        env[preferences.HOME_ENV] = str(env_home)
    proc = subprocess.run([*cmd, *args], capture_output=True, text=True, env=env, input=stdin, check=False)
    return proc.returncode, json.loads(proc.stdout)


SWARM = "- 用 swarm 派发探索子 Agent。\n- 每个 Worker 用 `gemini-4-hydrogen-low`，收齐简报后结束。"


class PreferencesTest(unittest.TestCase):
    def test_get_without_file_reports_unconfigured_and_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "prefs"
            code, payload = run_cli(home, "get", "oil-explore", "--host", "codex")
            self.assertEqual(code, 0)
            self.assertFalse(payload["configured"])
            self.assertEqual((payload["shared"], payload["host_text"]), ("", ""))
            self.assertFalse(home.exists())

    def test_set_per_host_and_for_all_hosts_then_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            code, payload = run_cli(home, "set", "oil-explore", "--host", "agy", "--text", SWARM)
            self.assertEqual(code, 0, payload)
            self.assertEqual(payload["host"], "antigravity")
            run_cli(home, "set", "oil-explore", "--host", "Claude Code", "--text", "- 用 Explore 子 Agent，模型 haiku。")
            run_cli(home, "set", "oil-explore", "--all-hosts", "--text", "探索子 Agent 最多 3 个。")

            _, agy = run_cli(home, "get", "oil-explore", "--host", "antigravity")
            self.assertEqual(agy["shared"], "探索子 Agent 最多 3 个。")
            self.assertEqual(agy["host_text"], SWARM)
            _, codex = run_cli(home, "get", "oil-explore", "--host", "codex")
            self.assertTrue(codex["configured"])
            self.assertEqual(codex["host_text"], "")

            text = (home / "preferences.md").read_text(encoding="utf-8")
            self.assertTrue(text.startswith("# oil-dev 个人偏好"))
            self.assertLess(text.index("最多 3 个"), text.index("### antigravity"))
            self.assertIn("### claude-code", text)

            run_cli(home, "set", "oil-explore", "--host", "antigravity", "--text", "- 改用 invoke_subagent。")
            _, agy = run_cli(home, "get", "oil-explore", "--host", "agy")
            self.assertEqual(agy["host_text"], "- 改用 invoke_subagent。")
            _, cc = run_cli(home, "get", "oil-explore", "--host", "cc")
            self.assertEqual(cc["host_text"], "- 用 Explore 子 Agent，模型 haiku。")

    def test_reads_hand_written_files_with_loose_headings_and_code_fences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "preferences.md").write_text(
                "# 我的偏好\n\n随便写点说明。\n\n"
                "## `oil-explore`（探索）\n\n所有宿主都最多派 2 个。\n\n"
                "### Antigravity（swarm）\n\n- 用 swarm。\n\n```text\n### codex\n不是标题\n```\n\n"
                "### Codex\n\n- 用默认子 Agent。\n\n"
                "## oil-plan\n\n- 方案写短一点。\n",
                encoding="utf-8",
            )
            _, agy = run_cli(home, "get", "oil-explore", "--host", "antigravity")
            self.assertEqual(agy["shared"], "所有宿主都最多派 2 个。")
            self.assertIn("```text\n### codex\n不是标题\n```", agy["host_text"])
            _, codex = run_cli(home, "get", "oil-explore", "--host", "codex")
            self.assertEqual(codex["host_text"], "- 用默认子 Agent。")
            _, plan = run_cli(home, "get", "oil-plan", "--host", "codex")
            self.assertEqual(plan["shared"], "- 方案写短一点。")

    def test_set_and_unset_leave_other_content_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            original = "# 我的偏好\n\n顶部说明。\n\n## oil-plan\n\n- 方案写短一点。\n"
            (home / "preferences.md").write_text(original, encoding="utf-8")
            run_cli(home, "set", "oil-explore", "--host", "codex", "--text", "- 用默认子 Agent。")
            text = (home / "preferences.md").read_text(encoding="utf-8")
            self.assertTrue(text.startswith(original.rstrip("\n")))

            code, payload = run_cli(home, "unset", "oil-explore", "--host", "codex")
            self.assertEqual(code, 0)
            self.assertTrue(payload["removed"])
            self.assertEqual((home / "preferences.md").read_text(encoding="utf-8"), original)

    def test_invalid_input_is_rejected_without_touching_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            run_cli(home, "set", "oil-explore", "--host", "codex", "--text", "- 用默认子 Agent。")
            before = (home / "preferences.md").read_text(encoding="utf-8")
            bad_calls = [
                ("set", "oil-explore", "--host", "codex", "--text", "   "),
                ("set", "oil-explore", "--host", "codex", "--text", "## 偷偷加一个标题"),
                ("set", "oil-explore", "--host", "codex", "--text", "x" * (preferences.MAX_TEXT_LENGTH + 1)),
                ("set", "oil-delegate", "--host", "codex", "--text", "x"),
                ("set", "oil-explore", "--host", "bad/host", "--text", "x"),
                ("get", "oil-explorer", "--host", "codex"),
            ]
            for call in bad_calls:
                code, payload = run_cli(home, *call)
                self.assertEqual(code, 2, call)
                self.assertEqual(payload["status"], "ERROR", call)
                self.assertTrue(payload["error"], call)
            self.assertEqual((home / "preferences.md").read_text(encoding="utf-8"), before)

    def test_env_home_stdin_text_and_show(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_home = Path(tmp) / "from-env"
            code, _ = run_cli(None, "set", "oil-explore", "--host", "agy", "--text", "-", env_home=env_home, stdin=SWARM)
            self.assertEqual(code, 0)
            self.assertTrue((env_home / "preferences.md").is_file())
            _, shown = run_cli(None, "show", env_home=env_home)
            self.assertTrue(shown["exists"])
            self.assertIn("gemini-4-hydrogen-low", shown["content"])
            self.assertIn("oil-explore", shown["skills"])


if __name__ == "__main__":
    unittest.main()
