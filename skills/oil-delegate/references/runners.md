# Runner 适配参考

只在首次配置、显式只读、特定 CLI 失败诊断和适配器维护时阅读相关小节。日常最高权限委托不需要阅读本文。返回主流程见 [SKILL.md](../SKILL.md)。

## 目录导航

- [共同约束](#共同约束)
- [Claude Code](#claude-code)
- [WorkBuddy](#workbuddy)
- [Antigravity](#antigravity)
- [Codex](#codex)
- [OpenCode](#opencode)
- [DeepSeek Harness](#deepseek-harness)
- [Grok Build](#grok-build)
- [Kimi Code](#kimi-code)
- [失败处理](#失败处理)

## 共同约束

正式 Runner 名为 `claude`、`workbuddy`、`antigravity`、`codex`、`opencode`、`dsh`、`grok`、`kimi`。别名包括 `cc/claude-code`、`wb/codebuddy/cbc`、`agy/anti`、`deepseek/deepseek-harness`、`grok-build`、`kimi-code`。名称不区分大小写；启用、默认 Runner、单次委托和二进制路径配置都使用同一套别名解析，保存时统一转为正式 Runner 名。

优先级：环境变量 `OIL_DELEGATE_<RUNNER>_BIN` → 配置中的 runner path → PATH → 已知安装目录。所有调用使用 argv 数组，prompt 不经过 shell。常规调用不指定模型、provider 或思考强度。

省略 `--mode` 时默认 `write`，使用各 CLI 的最高可用权限，尽量避免无人值守任务停在常规审批。只有用户明确要求只读、plan 或限制权限时才使用 `read`。权限参数只影响当前进程，不修改目标 CLI 的永久配置。

最高权限不等于安全隔离，也不保证所有操作一定成功：静态 deny、hooks、管理锁、登录、额度和网络仍可能失败。调用方仍需限定任务范围、检查改动并重跑测试。

## Claude Code

- 发现：`claude`。
- 新建：`claude -p <prompt> --output-format json`；续接增加 `--resume <session_id>`。
- read：`--permission-mode plan --permission-prompts none`。
- write：`--dangerously-skip-permissions`。
- 解析：最终 JSON 的 `session_id`、`result`、`is_error`。
- 认证：先用官方交互式 CLI 完成安装与登录；不要在包装器中输入或转存密钥。

官方参考：[Claude Code CLI usage](https://docs.anthropic.com/en/docs/claude-code/cli-usage)。

## WorkBuddy

- 发现：PATH 中的 `codebuddy` 或 `cbc`；macOS 还检测 WorkBuddy App 的 `Contents/Resources/app.asar.unpacked/cli/bin/codebuddy`。Runner 名仍为 `workbuddy`。
- 新建：`codebuddy -p <prompt> --output-format json`；续接增加 `--resume <session_id>`。
- read：`--permission-mode plan -y`。`-y` 让读取等常规授权可无人值守，plan 限制实施修改。
- write：`--permission-mode bypassPermissions -y`，并设置 `CODEBUDDY_IS_SANDBOX=1`，同时放行 HIGH/CRITICAL 操作。这是用户选择的最高权限模式，不代表进程实际处于隔离沙箱。
- 进程环境：所有模式设置 `CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS=1`，避免 single-shot 模式不支持的后台任务。
- 解析：与 Claude 兼容的最终 JSON 字段。
- 认证：复用 WorkBuddy/CodeBuddy 已有登录。GUI 自动化不是后备方案。

官方参考：[WorkBuddy Headless Mode](https://www.workbuddy.ai/docs/cli/headless)。

## Antigravity

- 发现：`agy`，Runner 名为 `antigravity`，接受 `agy`、`anti`。
- 新建：`agy -p <prompt> --output-format json`；续接增加 `--conversation <conversation_id>`。
- read：`--mode plan`。
- write：`--dangerously-skip-permissions`。
- 解析：`conversation_id`、`response`、`status`；只有 `SUCCESS` 可视为协议成功。
- 认证：headless 使用缓存凭据，需先通过官方交互式 `agy` 完成认证；未认证时应报错而不是等待输入。

官方参考：[Antigravity Headless Mode](https://antigravity.google/docs/cli/headless/)。

## Codex

- 发现：`codex`。
- 新建：`codex exec --cd <workspace> --json --skip-git-repo-check ... -`，prompt 从 stdin 传入。
- read：新会话使用 `--sandbox read-only`；续接使用 `-c sandbox_mode="read-only"`。
- write：`--dangerously-bypass-approvals-and-sandbox`。
- 续接：`codex exec resume --json ... <thread_id> -`，read 续接保留只读沙箱，write 续接使用最高权限。
- 解析：JSONL 中的 `thread.started`、`item.completed` agent message、`turn.failed` 和 `error`。
- 认证：沿用 Codex CLI 默认配置与登录，不复制其他目录的 provider 设置。

官方参考：[Codex CLI](https://github.com/openai/codex)。

## OpenCode

- 发现：`opencode`，也可用 `OIL_DELEGATE_OPENCODE_BIN` 指向受信任 sidecar。
- 新建：`opencode run --format json --dir <workspace> <prompt>`；续接增加 `--session <session_id>`。
- read：增加 `--agent plan`，并设置进程级 `OPENCODE_PERMISSION`，禁止 edit、bash、external_directory 和 question。
- write：进程级权限允许 edit、bash 和 external_directory；question 仍拒绝，避免无交互任务等待用户。该方式不依赖新版才有的 `--auto`，同时适用于 1.3.9。
- 解析：事件及其 `part` 中的 `sessionID/sessionId/session_id`；连接 text 事件的 `part.text`，读取 error 事件。
- 认证：使用 `opencode auth login` 或其现有 provider 配置；包装器不读取认证文件。

官方参考：[OpenCode CLI](https://opencode.ai/docs/cli/) 与 [Permissions](https://opencode.ai/docs/permissions/)。

## DeepSeek Harness

- 发现：`dsh`，Runner 名为 `dsh`。
- 新建：`dsh --profile headless --json -`，prompt 从 stdin 传入；续接增加 `--session-id <sessionId>`。
- read：支持新 flags 时使用 `--permission-mode read-only`；否则设置进程环境 `DSH_PERMISSION_MODE=read-only`。
- write：使用 `--permission-mode danger-full-access` 或同名环境变量回退，不再限制为 workspace-write。支持时增加 `--approval-policy never`。
- 能力探测：onboarding 执行 `dsh --profile headless --help`，分别检查 `--json`、`--session-id`、`--permission-mode`、`--approval-policy`。缺少 JSON 时不接受任务，缺少 session flag 时不接受续接。
- 解析：开头 `session.sessionId`，结尾 `final.text` 与可用的 `final.reason`，以及 `error`。部分版本把终止原因放在 `status` 的 `turn_end` 中且让 final 省略 reason，解析器同时兼容；已知 reason 不是 completed 时即使进程退出 0 也判为失败。
- 续接限制：保持原 cwd，并尊重 DSH 对 cwd、ownership 和 preset 的校验；不得把拒绝错误绕过为新空会话。
- 认证：headless 的凭据链由实际 DSH profile 决定。包装器不读取 `.env`，也不假设 GUI 登录一定可用于 headless；根据真实错误引导用户使用官方配置。

官方参考：[DSH Headless](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md)。

## Grok Build

- 发现：`grok`，Runner 名为 `grok`，接受 `grok-build`。
- 新建：`grok --cwd <workspace> -p <prompt> --output-format json`；续接增加 `--resume <sessionId>`。`--session-id` 只用于新建指定 UUID，不用于续接。
- read：`--permission-mode plan`。
- write：`--always-approve`，自动批准编辑和命令；deny rules、hooks 和 admin locks 仍然有效。
- 解析：最终 JSON 的 `sessionId`、`text`、`stopReason`；错误对象使用 `type/message`。
- 认证：复用 Grok 已有登录；无浏览器环境由用户通过官方 device-auth 或 XAI 凭据链路另行配置。

官方参考：[Grok Build Headless Mode](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md)。

## Kimi Code

- 发现：`kimi`，Runner 名为 `kimi`，接受 `kimi-code`。
- 新建：`kimi -p <prompt> --output-format stream-json`；续接增加 `-S <session_id>`。
- write：`-p` 自身已经使用 auto 权限，不再添加 `--auto`。官方明确禁止 `-p` 与 `--auto`、`--yolo` 或 `--plan` 同用。
- read：不支持。非交互模式自带 auto 权限，无法保证只读；收到 read 请求时明确拒绝，不伪装成 plan。
- 解析：assistant 的文本 content 和 `session.resume_hint.session_id`；同时兼容事件中的 session 字段与 error。
- 认证：复用 Kimi Code 已有登录；静态 deny 规则仍然有效，登录由官方 CLI 另行完成。

官方参考：[Kimi Command](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-command.html)。

## 失败处理

- **找不到命令**：安装目标 CLI，或通过 `config --set-bin <runner>=<path>` 指定真实可执行文件；不自动启用其他 Runner。
- **未认证/额度不足**：停止并报告目标 CLI 的官方修复入口，不读取或打印凭据。最高权限不能解决登录或额度问题。
- **权限拒绝**：先检查静态 deny、hooks、管理锁和实际 mode。显式 read 不自动升级为 write；默认 write 也不绕过目标产品的强制限制。
- **输出无法解析**：同时保留协议错误、stderr、原始 stdout 和退出码；先确认实际 CLI 版本与输出格式，不把空响应当成功。
- **超时**：保留部分事件与结果，清理进程后不盲目重试。先缩小任务或确认是否卡在认证、权限或网络。
- **续接失败**：核对 Runner、session ID、cwd 和会话归属；跨 Runner 必须新建任务。
