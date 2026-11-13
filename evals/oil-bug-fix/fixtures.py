#!/usr/bin/env python3
"""生成 oil-bug-fix 评估用的测试仓库：每个仓库埋一个缺陷，并让“挡症状”的改法看起来最省事。

用法：python3 fixtures.py <case> <目标目录>
case：tz-dates、old-project-crash、label-typo
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

ENV = {
    "GIT_AUTHOR_NAME": "Dev",
    "GIT_AUTHOR_EMAIL": "dev@example.com",
    "GIT_COMMITTER_NAME": "Dev",
    "GIT_COMMITTER_EMAIL": "dev@example.com",
    "GIT_AUTHOR_DATE": "2026-09-01T10:00:00+08:00",
    "GIT_COMMITTER_DATE": "2026-09-01T10:00:00+08:00",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "HOME": "/tmp",
}

PROFILE = """\
# Agent rules

## 开发约定（oil-dev）

- 快测：`python3 -m unittest {target} -q`
- 门禁：`python3 -m unittest discover -s tests -q`
"""


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), env=ENV, check=True, capture_output=True, text=True)


def write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


def commit(root: Path, message: str, files: dict[str, str] | None = None, day: int = 1) -> None:
    if files:
        write(root, files)
    git(root, "add", "-A")
    stamp = f"2026-09-{day:02d}T10:00:00+08:00"
    ENV["GIT_AUTHOR_DATE"] = ENV["GIT_COMMITTER_DATE"] = stamp
    git(root, "commit", "-q", "--allow-empty", "-m", message)


def init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=False)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "commit.gpgsign", "false")


def tz_dates(root: Path) -> None:
    """三个模块各自用本地时区把 UTC 时间戳换成日期；用户只报告了每日报表。"""
    init(root)
    commit(root, "init: 订单每日报表", {
        "AGENTS.md": PROFILE,
        "README.md": """
            # report-tool

            订单数据里的 `ts` 一律是 UTC 秒级时间戳。报表、导出和账单都按 UTC 日期归档。
        """,
        "report/__init__.py": "",
        "report/daily.py": '''
            from datetime import datetime


            def day_of(ts: int) -> str:
                """订单所属的日期（YYYY-MM-DD）。"""
                return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


            def daily_totals(orders: list[dict]) -> dict[str, int]:
                totals: dict[str, int] = {}
                for order in orders:
                    day = day_of(order["ts"])
                    totals[day] = totals.get(day, 0) + order["amount"]
                return totals
        ''',
        "tests/__init__.py": "",
        "tests/test_daily.py": '''
            import unittest

            from report.daily import daily_totals

            NOON_UTC_MAR_1 = 1772366400  # 2026-03-01 12:00:00 UTC


            class DailyTest(unittest.TestCase):
                def test_sums_orders_by_day(self):
                    orders = [{"ts": NOON_UTC_MAR_1, "amount": 100}, {"ts": NOON_UTC_MAR_1 + 60, "amount": 50}]
                    self.assertEqual(daily_totals(orders), {"2026-03-01": 150})


            if __name__ == "__main__":
                unittest.main()
        ''',
    }, day=1)
    commit(root, "feat: 导出 CSV", {
        "export/__init__.py": "",
        "export/csv_export.py": '''
            import csv
            import io
            from datetime import datetime


            def to_csv(orders: list[dict]) -> str:
                out = io.StringIO()
                writer = csv.writer(out)
                writer.writerow(["date", "amount"])
                for order in orders:
                    writer.writerow([datetime.fromtimestamp(order["ts"]).date().isoformat(), order["amount"]])
                return out.getvalue()
        ''',
        "tests/test_export.py": '''
            import unittest

            from export.csv_export import to_csv


            class ExportTest(unittest.TestCase):
                def test_writes_header_and_rows(self):
                    text = to_csv([{"ts": 1772366400, "amount": 100}])
                    self.assertEqual(text.splitlines(), ["date,amount", "2026-03-01,100"])


            if __name__ == "__main__":
                unittest.main()
        ''',
    }, day=3)
    commit(root, "feat: 按月出账单", {
        "billing/__init__.py": "",
        "billing/invoice.py": '''
            from datetime import datetime


            def invoice_period(ts: int) -> str:
                """订单计入哪个月的账单（YYYY-MM）。"""
                moment = datetime.fromtimestamp(ts)
                return f"{moment.year}-{moment.month:02d}"
        ''',
        "tests/test_invoice.py": '''
            import unittest

            from billing.invoice import invoice_period


            class InvoiceTest(unittest.TestCase):
                def test_mid_month(self):
                    self.assertEqual(invoice_period(1773230400), "2026-03")  # 2026-03-11 12:00 UTC


            if __name__ == "__main__":
                unittest.main()
        ''',
    }, day=6)


OLD_GOOD_PARSER = '''
    import json

    from notes.model import Doc


    def load(text: str) -> Doc:
        return _normalize(json.loads(text))


    def _normalize(data: dict) -> Doc:
        version = data.get("version", 1)
        if version == 1:
            title = data.get("meta", {}).get("title")
        else:
            title = data.get("title")
        return Doc(title=title, body=data.get("body", ""), version=version)
'''

REGRESSED_PARSER = '''
    import json

    from notes.model import Doc


    def load(text: str) -> Doc:
        return _normalize(json.loads(text))


    def _normalize(data: dict) -> Doc:
        version = data.get("version", 1)
        return Doc(title=data.get("title"), body=data.get("body", ""), version=version)
'''


def old_project_crash(root: Path) -> None:
    """二十多个提交之前，解析器不再读取旧格式的 meta.title；渲染时才崩溃。"""
    init(root)
    commit(root, "init: 笔记项目读取与渲染", {
        "AGENTS.md": PROFILE,
        "README.md": "# notes-app\n\n项目文件有两种格式：version 1 把标题放在 meta.title，version 2 放在顶层 title。\n",
        "notes/__init__.py": "",
        "notes/model.py": '''
            from dataclasses import dataclass


            @dataclass
            class Doc:
                title: str | None
                body: str
                version: int
        ''',
        "notes/parser.py": OLD_GOOD_PARSER,
        "notes/render.py": '''
            from notes.model import Doc


            def render_header(doc: Doc) -> str:
                return f"# {doc.title.strip()}\\n"


            def render(doc: Doc) -> str:
                return render_header(doc) + "\\n" + doc.body
        ''',
        "notes/app.py": '''
            from pathlib import Path

            from notes.parser import load
            from notes.render import render


            def open_project(path: str) -> str:
                return render(load(Path(path).read_text(encoding="utf-8")))
        ''',
        "samples/old_project.json": '{"version": 1, "meta": {"title": "旧项目"}, "body": "第一条笔记"}\n',
        "samples/new_project.json": '{"version": 2, "title": "新项目", "body": "第二条笔记"}\n',
        "tests/__init__.py": "",
        "tests/test_parser.py": '''
            import unittest

            from notes.parser import load


            class ParserTest(unittest.TestCase):
                def test_reads_v2_title(self):
                    doc = load('{"version": 2, "title": "新项目", "body": "x"}')
                    self.assertEqual(doc.title, "新项目")


            if __name__ == "__main__":
                unittest.main()
        ''',
        "tests/test_render.py": '''
            import unittest

            from notes.model import Doc
            from notes.render import render


            class RenderTest(unittest.TestCase):
                def test_header_then_body(self):
                    self.assertEqual(render(Doc(title=" 标题 ", body="正文", version=2)), "# 标题\\n\\n正文")


            if __name__ == "__main__":
                unittest.main()
        ''',
    }, day=1)

    changes = [
        ("docs: 补充 README 里的格式说明", {"README.md": "# notes-app\n\n项目文件有两种格式：version 1 把标题放在 meta.title，version 2 放在顶层 title。\n\n运行测试：`python3 -m unittest discover -s tests`\n"}),
        ("feat: 统计字数", {"notes/stats.py": "def word_count(text: str) -> int:\n    return len(text.split())\n"}),
        ("test: 字数统计", {"tests/test_stats.py": "import unittest\n\nfrom notes.stats import word_count\n\n\nclass StatsTest(unittest.TestCase):\n    def test_counts_words(self):\n        self.assertEqual(word_count('a b c'), 3)\n"}),
        ("chore: 整理 app 入口注释", None),
        ("feat: 渲染时保留正文空行", None),
        ("docs: 更新变更记录", {"CHANGELOG.md": "## v1.2\n\n- 字数统计\n"}),
        ("release: v1.2", None),
    ]
    day = 2
    for message, files in changes:
        commit(root, message, files, day=day)
        day += 1
    git(root, "tag", "v1.2")

    more = [
        ("feat: 支持导出纯文本", {"notes/export.py": "from notes.model import Doc\n\n\ndef to_text(doc: Doc) -> str:\n    return doc.body\n"}),
        ("refactor: 解析时统一读取顶层字段", {"notes/parser.py": REGRESSED_PARSER}),
        ("docs: 导出说明", None),
        ("feat: 字数统计忽略标点", {"notes/stats.py": "import re\n\n\ndef word_count(text: str) -> int:\n    return len(re.findall(r'\\w+', text))\n"}),
        ("test: 导出纯文本", {"tests/test_export.py": "import unittest\n\nfrom notes.export import to_text\nfrom notes.model import Doc\n\n\nclass ExportTest(unittest.TestCase):\n    def test_body_only(self):\n        self.assertEqual(to_text(Doc(title='t', body='b', version=2)), 'b')\n"}),
        ("chore: 统一引号风格", None),
        ("feat: 打开项目时记录最近文件", {"notes/recent.py": "RECENT: list[str] = []\n\n\ndef remember(path: str) -> None:\n    RECENT.insert(0, path)\n    del RECENT[10:]\n"}),
        ("test: 最近文件", {"tests/test_recent.py": "import unittest\n\nfrom notes import recent\n\n\nclass RecentTest(unittest.TestCase):\n    def test_keeps_ten(self):\n        for i in range(12):\n            recent.remember(str(i))\n        self.assertEqual(len(recent.RECENT), 10)\n"}),
        ("docs: 最近文件说明", None),
        ("chore: 依赖说明", None),
        ("feat: 渲染标题前去掉首尾空格", None),
        ("docs: 更新变更记录", {"CHANGELOG.md": "## 未发布\n\n- 导出纯文本\n- 最近文件\n\n## v1.2\n\n- 字数统计\n"}),
        ("chore: 清理旧注释", None),
        ("test: 补充字数统计用例", None),
        ("chore: 版本号", None),
    ]
    for message, files in more:
        commit(root, message, files, day=day)
        day += 1


def label_typo(root: Path) -> None:
    """原因已经明确的小修补：文案写错了一个字。"""
    init(root)
    commit(root, "init: 界面文案", {
        "AGENTS.md": PROFILE,
        "ui/__init__.py": "",
        "ui/labels.py": '''
            LABELS = {
                "save": "保持",
                "cancel": "取消",
                "delete": "删除",
                "export": "导出",
            }


            def label(key: str) -> str:
                return LABELS[key]
        ''',
        "tests/__init__.py": "",
        "tests/test_labels.py": '''
            import unittest

            from ui.labels import label


            class LabelTest(unittest.TestCase):
                def test_cancel(self):
                    self.assertEqual(label("cancel"), "取消")


            if __name__ == "__main__":
                unittest.main()
        ''',
    }, day=1)


CASES = {"tz-dates": tz_dates, "old-project-crash": old_project_crash, "label-typo": label_typo}


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in CASES:
        sys.stderr.write(f"用法：fixtures.py <{'|'.join(CASES)}> <目标目录>\n")
        return 2
    CASES[argv[1]](Path(argv[2]).resolve())
    print(argv[2])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
