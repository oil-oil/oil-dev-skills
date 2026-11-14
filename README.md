English · [中文](README.zh-CN.md)

<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="oil-dev-skills: a development workflow for AI coding agents. Explore and plan on main; delegate implementation and run independent code review in a worktree; pass the merge gate back to main, then govern. After exploration, a separate bug-fix branch finds the root cause before fixing and merges back to main.">
</p>

`oil-dev-skills` is a set of six Agent Skills that work together. Once installed in a skill-compatible coding agent such as Claude Code or Codex, you can invoke them in plain language. They break a change into five steps: map the existing code, write a plan you can actually read, hand off implementation under a clear contract, review the diff in a clean context, and tidy up docs, tests, and worktrees at the end of a milestone. For bugs with an unknown cause, a dedicated path finds the root cause before changing production code.

Each skill also works on its own. Small changes don't need the full workflow—the agent explains the change, edits the code directly, and runs the relevant quick tests.

## What problems it solves

As a project grows, asking an AI agent to edit code directly often runs into the same issues. Each one maps to a specific practice in this workflow:

- **Writing duplicate helpers before understanding the existing code.** `oil-explore` traces call chains in read-only mode first and lists existing components and functions you can reuse.
- **Plans that read like low-level implementation dumps.** `oil-plan` explains changes in terms of what changes externally, what gets reused, and what is out of scope this time, pulling out the trade-offs that need your call.
- **Running the full test suite after every two lines of code.** The dispatch contract limits mid-coding checks to second-level compile, typecheck, and lint runs, followed by quick tests related to the change once coding is done. Full test suites and builds wait until the merge gate.
- **Diffs that drift beyond the agreed plan or touch restricted directories.** Before review starts, a helper script compares the diff against the confirmed plan and project conventions and flags those files upfront.
- **Patching symptoms while the same bug pattern remains elsewhere.** `oil-bug-fix` reproduces the failure and traces the root cause so the fix happens where bad data originates. It then checks whether the issue is systemic—if so, it scans the repo, fixes matching cases together, and proposes a deterministic check or review rule so it doesn't come back.
- **`AGENTS.md` drifting out of sync with the code and stale worktrees piling up.** `oil-govern` checks documentation links and freshness, test tiering, and worktree merge status before proposing cleanup.

## Check scope before reviewing

Here is a real run on a sample repository. Its `AGENTS.md` marks `src/storage/` as high-risk and `vendor/` as do-not-edit, while the confirmed plan only allows changes under `src/export/**` and `tests/**`. Once implementation finishes, `oil-code-review`'s helper script flags three groups of files before the review itself begins:

<p align="center">
  <img src="./assets/readme/review-check.svg" width="100%" alt="On the left are the dev profile in AGENTS.md and the allowed scope from the confirmed plan. On the right is the output from review_helper.py: src/storage/project_file.py falls under a high-risk path, vendor/ffmpeg/wrap.py falls under a do-not-edit path, and 3 files fall outside the plan's allowed scope.">
</p>

The review then handles each group differently:

- **High-risk paths** go to an agent with a clean context for an independent deep review.
- **Do-not-edit paths** are treated as blocking issues unless the confirmed plan explicitly allowed them.
- **Files outside the plan scope** are checked one by one: necessary additions are explained to you, while opportunistic drive-by edits are marked as blocking.

## The six skills

- **Explore · [`oil-explore`](skills/oil-explore/SKILL.md)**: Takes a read-only inventory of directory size, convention files, and working test and build commands. On larger repositories, it dispatches read-only sub-agents by module to trace call chains in parallel.
- **Plan · [`oil-plan`](skills/oil-plan/SKILL.md)**: Scales across three levels. Small edits go straight to code; regular features align on a plan in chat; cross-module changes become a written spec that can fan out across multiple worktrees. After implementation, it verifies the result and merges through a closing checklist.
- **Delegate · [`oil-delegate`](skills/oil-delegate/SKILL.md)**: Hands off a well-bounded task to locally installed CLI agents such as Claude Code, Codex, or Antigravity, returns structured results to your current session, and lets you follow up in the same thread. It can remember which agent you prefer for code review, plan critique, debugging consultations, and implementation.
- **Review · [`oil-code-review`](skills/oil-code-review/SKILL.md)**: Reviews code diffs only. It checks scope drift against the plan first, then grades by risk: lightweight checks for minor UI tweaks, and a five-part deep review in a clean context for core logic and high-risk changes—covering end-to-end business and build verification, reuse, redundancy, runtime failure modes, and security and compatibility.
- **Bug Fix · [`oil-bug-fix`](skills/oil-bug-fix/SKILL.md)**: Handles bugs whose cause isn't obvious yet. It reproduces the issue with a failing test, locates the root cause through git bisect, backward value tracing, or controlled experiments, and fixes where the bad state starts. If the pattern is systemic, it scans the repo to fix matching spots together and proposes an automated check or review rule. Small fixes with a known cause go straight through `oil-plan`.
- **Govern · [`oil-govern`](skills/oil-govern/SKILL.md)**: Audits documentation, test tiers, and git worktrees, helps generate and maintain your project's dev profile, and turns hard project rules into deterministic repo checks.

## Installation

Send this to your agent:

```text
Please install this skill repository for me: https://github.com/oil-oil/oil-dev-skills
```

Or install the full set from the terminal:

```bash
npx skills add oil-oil/oil-dev-skills
```

To install a single skill, pass `--skill`:

```bash
npx skills add oil-oil/oil-dev-skills --skill oil-code-review
```

No extra configuration is required after installation:

- The scripts in `oil-explore`, `oil-plan`, `oil-code-review`, and `oil-govern` only inspect Git state and file structure. They need no API keys and work with any programming language.
- On first use, `oil-delegate` detects which CLI agents are installed on your machine and asks which ones you want to enable.
- When a task type such as code review or plan critique comes up for the first time, `oil-delegate` asks once which agent should handle it and remembers your choice. You can also say things like "Always use codex for reviews from now on." These preferences live in `oil-delegate`'s local config and are never written into the repository.
- If you previously installed [oil-delegate](https://github.com/oil-oil/oil-delegate) on its own, keep only one copy after installing the full set so you don't end up with duplicate skill names.

## Usage

Tell your agent what you want in plain language. A cross-module feature typically follows this order:

```text
Use oil-explore to trace the full video export call chain in this repo and list existing components and helpers we can reuse.
```

```text
I want to add multi-select batch splitting to the timeline. Use oil-plan to write a plan I can easily follow and list the trade-offs that need my decision. Don't change any code yet.
```

```text
The plan looks good. Use oil-delegate to have codex implement it according to the plan and run the relevant quick tests when done.
```

```text
Use oil-code-review to review the changes on the current branch, focusing on core logic and packaged runtime behavior.
```

```text
Use oil-govern to audit the repository—check for stale docs, review the test tiering, and see which worktrees can be cleaned up.
```

Once review passes, `oil-plan` runs the merge gate from its closing checklist, commits and merges according to the repo's conventions, and cleans up the merged worktree.

When you run into a bug whose cause isn't clear, describe the symptom directly:

```text
Use oil-bug-fix to investigate: overseas users report that some orders in the daily report are counted under the previous day, and we haven't been able to reproduce it locally.
```

## Project dev profile (recommended)

Add a section whose heading contains `oil-dev` to the target repository's `AGENTS.md` to record your quick test command, merge gate, high-risk paths, and do-not-edit paths:

```markdown
## Dev profile (oil-dev)

- quick-test: `pytest tests/{target} -q`
- merge-gate: `make ci`
- high-risk: `src/storage/**`, `src/auth/**`, `migrations/`
- do-not-edit: `vendor/`, `src/generated/**`
```

Once added, the skills read it directly: `oil-explore` stops guessing test commands, `oil-plan` copies `quick-test` and `merge-gate` into the dispatch contract, and `oil-code-review` flags changes in `high-risk` and `do-not-edit` paths as shown above. Without this section, each skill falls back to its built-in defaults.

If you'd rather not write it by hand, tell your agent: "Use oil-govern to generate a dev profile for this repo." See [dev-profile.md](skills/oil-govern/references/dev-profile.md) for the format and generation steps.

## Personal preferences

Settings that apply only to you live in `~/.oil-dev/preferences.md` on your machine. All skills in this collection share that single file, with one section per skill written in plain language. Notes directly under a skill heading apply across all host agents, while notes under `### <host-name>` apply only to that host. This file stays out of project repositories; team-wide conventions still belong in the dev profile above.

Currently, `oil-explore` reads this file when fanning out parallel exploration. You can specify how each host should dispatch exploration sub-agents: which mechanism to use, which model to pick, the maximum number of sub-agents, and how to clean them up afterward.

```markdown
## oil-explore

At most 3 exploration sub-agents.

### antigravity

- Dispatch exploration sub-agents with swarm, using `<model-id>` for each worker.
- Kill the workers once all briefs are collected.

### claude-code

- Use the Explore sub-agent with the haiku model.
```

You can edit the file directly or tell your agent to save a preference for you:

```text
From now on, when using oil-explore in Claude Code, run exploration sub-agents on haiku and dispatch at most 3 of them.
```

Anything left unspecified uses the default: 2–3 sub-agents on a fast, lightweight model. Preferences for `oil-delegate` (which CLI agents are enabled and which agent handles each task type) are stored in its own config file rather than here.

## Permissions and boundaries

- `oil-explore` is strictly read-only. `oil-code-review` and `oil-govern` are also read-only while gathering facts; before editing docs, deleting tests, or removing worktrees, they show the evidence and wait for your confirmation.
- `oil-plan` does not start cross-module code edits until open trade-offs have your sign-off.
- While isolating a root cause, `oil-bug-fix` only adds tests and temporary diagnostic logs without altering production behavior; it asks before adding automated checks or updating `AGENTS.md` or `REVIEW.md`.
- When `oil-delegate` runs code reviews, plan critiques, or debugging consultations, it uses read-only mode automatically and verifies afterward that the working tree is untouched. Before dispatching a task that writes code, it requires a clean working tree or an isolated worktree.
- Prompts and files handed to external CLI agents are sent to their respective model providers and may incur usage costs. Do not include secrets or credentials in task prompts.
- All helper scripts rely only on the Python 3.10+ standard library. They have been tested on macOS; Linux and Windows have not yet been verified on physical machines.
- This collection does not handle packaging or release publishing. Signing, notarization, and deployment are product-specific and are best kept as scripts in the target repository.

<details>
<summary>Maintenance and testing</summary>

Run all unit tests without calling any models (finishes in under a minute):

```bash
python3 scripts/verify_repo.py
```

If you have [oil-skill-creator](https://github.com/oil-oil/oil-skill-creator) installed, pass `--validator <path-to-oil-skill-creator>/scripts/validate_skill.py` to also check each skill against public authoring guidelines.

`skills/oil-delegate` stays in sync with the standalone [oil-delegate](https://github.com/oil-oil/oil-delegate) repository. When maintaining both, verify they match with:

```bash
python3 scripts/sync_delegate.py --check
```

</details>

## License

[MIT](LICENSE)
