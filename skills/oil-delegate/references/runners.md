# Runner 适配参考

只在首次配置、显式只读、特定 CLI 失败诊断和适配器维护时阅读相关小节。日常最高权限委托不需要阅读本文。返回主流程见 [SKILL.md](../SKILL.md)。

## 目录导航

- [共同约束](#共同约束)
- [会话发现约束](#会话发现约束)
- [会话导出约束](#会话导出约束)
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

正式 Runner 名为 `claude`、`workbuddy`、`antigravity`、`codex`、`opencode`、`dsh`、`grok`、`kimi`。别名包括 `cc/claude-code`、`wb/codebuddy/cbc`、`agy/anti`、`deepseek/deepseek-harness`、`grok-build`、`kimi-code`。名称不区分大小写；启用、默认 Runner、单次委托、会话搜索和二进制路径配置都使用同一套别名解析，保存时统一转为正式 Runner 名。

优先级：环境变量 `OIL_DELEGATE_<RUNNER>_BIN` → 配置中的 runner path → PATH → 已知安装目录。所有调用使用 argv 数组，prompt 不经过 shell。常规调用不指定模型、provider 或思考强度。

省略 `--mode` 时默认 `write`，使用各 CLI 的最高可用权限，尽量避免无人值守任务停在常规审批。只有用户明确要求只读、plan 或限制权限时才使用 `read`。权限参数只影响当前进程，不修改目标 CLI 的永久配置。

最高权限不等于安全隔离，也不保证所有操作一定成功：静态 deny、hooks、管理锁、登录、额度和网络仍可能失败。调用方仍需限定任务范围、检查改动并重跑测试。

## 会话发现约束

`sessions [--runner <名称或别名>] [--query <标题关键词>] [--limit N] [--json]` 独立于启用池和默认 runner。只读取标题、ID、目录和更新时间，不启动任务、不调用模型、不联网。搜索不区分大小写，完全相等的标题优先，其余按更新时间倒序；全局限制默认 20。`source` 区分 `official`、`local-store` 与 `desktop-store`；后两者属于产品内部格式，升级后可能变化。每条结果包含布尔值 `resumable`；值为 false 时 `resume_command` 为 null。它表示来源是否支持 CLI 续接，不保证登录、权限或数据仍然有效。

| 数据根目录 | 默认值 | 环境变量覆盖 |
| --- | --- | --- |
| Kimi Code | `~/.kimi-code` | 官方 `KIMI_CODE_HOME`，传给列表子进程 |
| Codex | `~/.codex` | 官方 `CODEX_HOME` |
| Antigravity CLI | `~/.gemini/antigravity-cli` | `OIL_DELEGATE_ANTIGRAVITY_CLI_HOME` |
| Antigravity | `~/.gemini/antigravity` | `OIL_DELEGATE_ANTIGRAVITY_HOME` |
| Claude 桌面元数据（macOS） | `~/Library/Application Support/Claude/claude-code-sessions` | `OIL_DELEGATE_CLAUDE_DESKTOP_HOME` |
| Claude Code 数据 | `~/.claude` | `CLAUDE_CONFIG_DIR` |

仅在这些本地根目录内读取，拒绝经过符号链接、云目录或挂载视图；遍历时跳过符号链接目录与文件。不扫描工作目录本身。缺失的本地索引/数据库返回空列表；缺少官方 CLI 或来源读取失败写入 `errors[runner]`。某个 Antigravity 数据库失败仍保留另一个数据库的结果。无法可靠发现的 runner 列入 `unsupported`，不会尝试读取对话文件来猜标题。

单行元数据无效时跳过该行，保留同一来源中的其他会话，不写入 `errors`；缺失、非字符串或空字符串 ID 均无效。`title: null` 或缺失标题统一为空字符串，保留会话，有非空 `--query` 时自然不匹配。JSON 的 `skipped` 按 runner 统计筛选和限量之前跳过的坏行数，无坏行时为 `{}`；去重、未匹配标题和超出限量的会话不计入跳过数。

## 会话导出约束

`sessions export --runner <名称或别名> --session-id <ID> --out <Markdown> [--max-turns N] [--force] [--json]` 不依赖 onboarding 或 CLI 安装，不启动 CLI、不调用模型、不联网。只支持 Antigravity 与 Claude；其他 runner 返回 `status: ERROR`、说明原因、退出 1。

导出标题、工作目录、会话/导出时间与按时间排序的文字。每条用户消息或 Agent 文字回复计一轮，`turns` 为实际保留轮数；`omitted_turns` 为省略的较早轮数。默认保留最近 30 轮，可用正整数 `--max-turns` 调整；截断提示在标题之后。时间缺失时保留来源顺序，并接在此前的已知时间后。没有标题时使用 ID，缺失目录为 null，缺失时间标为未知。JSON 通过 `emit()` 输出 `status/path/title/workspace/turns/omitted_turns/runner/session_id` 与 `contract_version: 1`，不含正文。

输出父目录须存在，默认独占创建，已有文件不会被覆盖；`--force` 使用临时文件替换，拒绝符号链接输出及 CLI 数据目录。源目录和所选正文文件均排除符号链接、挂载、云目录与 macOS dataless 占位文件。不会复制、导入或修改原始会话。正文格式损坏、未知 Antigravity step_format 或必要字段不匹配时明确报错，不生成伪造的交接文本。

## Claude Code

- 发现：`claude`。
- 新建：`claude -p <prompt> --output-format json`；续接增加 `--resume <session_id>`。
- read：`--permission-mode plan --permission-prompts none`。
- write：`--dangerously-skip-permissions`。
- 解析：最终 JSON 的 `session_id`、`result`、`is_error`。
- 认证：先用官方交互式 CLI 完成安装与登录；不要在包装器中输入或转存密钥。
- 会话发现：macOS 的 `desktop-store`，固定两层读取 `claude-code-sessions/<a>/<b>/local_<id>.json` 的 `cliSessionId`、`cwd`、`title`、`lastActivityAt`，跳过 `isArchived`。使用 `cliSessionId` 作为 `session_id`，不使用桌面的 `sessionId` 或文件名 ID。同 CLI ID 保留最新元数据。非 macOS 或元数据目录不存在时安静返回空列表。
- 续接判断：将 cwd 中每个非 ASCII 字母、数字或连字符替换为 `-`，定位 `$CLAUDE_CONFIG_DIR/projects/<编码 cwd>/<cliSessionId>.jsonl`。仅检查该文件存在，不读正文；存在时 `resumable=true`，生成 `claude --resume <cliSessionId>`，否则 false/null。依据是本地桌面元数据与对应 JSONL 的实际关联，不意味着可以导入缺失的正文。
- 导出：优先使用桌面元数据定位正文；没有匹配时只枚举 `projects` 的一层项目目录，查找指定 `<session_id>.jsonl`，多个项目命中则报歧义，不递归读取 subagents。只读取 `type=user/assistant` 的 `message.content` 字符串或 `type=text` 块；跳过 `tool_use`、`tool_result`、thinking、图片及其他非文字块，排除 `isSidechain/isMeta/isCompactSummary`。相同 uuid 保留最后记录。无桌面标题时采用指定 JSONL 的最新 custom-title，其次 ai-title；不从 prompt 猜标题。CLI 来源不依赖桌面平台，可直接按 ID 导出。
- 稳定性：桌面 JSON 与 JSONL 均为内部格式，本机 2.1.314 的字段已离线核对。没有可靠的独立 CLI 历史标题索引，故列表仍不扫描 CLI 正文；仅桌面元数据覆盖的会话可被发现。

官方参考：[Claude Code CLI usage](https://docs.anthropic.com/en/docs/claude-code/cli-usage)。

## WorkBuddy

- 发现：PATH 中的 `codebuddy` 或 `cbc`；macOS 还检测 WorkBuddy App 的 `Contents/Resources/app.asar.unpacked/cli/bin/codebuddy`。Runner 名仍为 `workbuddy`。
- 新建：`codebuddy -p <prompt> --output-format json`；续接增加 `--resume <session_id>`。
- read：`--permission-mode plan -y`。`-y` 让读取等常规授权可无人值守，plan 限制实施修改。
- write：`--permission-mode bypassPermissions -y`，并设置 `CODEBUDDY_IS_SANDBOX=1`，同时放行 HIGH/CRITICAL 操作。这是用户选择的最高权限模式，不代表进程实际处于隔离沙箱。
- 进程环境：所有模式设置 `CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS=1`，避免 single-shot 模式不支持的后台任务。
- 解析：与 Claude 兼容的最终 JSON 字段。
- 认证：复用 WorkBuddy/CodeBuddy 已有登录。GUI 自动化不是后备方案。
- 会话发现：`unsupported`。本机帮助中的 `ps` 仅列活动会话，未确认覆盖已结束历史会话的纯标题列表接口。稳定性：未接入，不猜测 Claude 兼容存储格式。

官方参考：[WorkBuddy Headless Mode](https://www.workbuddy.ai/docs/cli/headless)。

## Antigravity

- 发现：`agy`，Runner 名为 `antigravity`，接受 `agy`、`anti`。
- 新建：`agy -p <prompt> --output-format json`；续接增加 `--conversation <conversation_id>`。
- read：`--mode plan`。
- write：`--dangerously-skip-permissions`。
- 解析：`conversation_id`、`response`、`status`；只有 `SUCCESS` 可视为协议成功。
- 认证：headless 使用缓存凭据，需先通过官方交互式 `agy` 完成认证；未认证时应报错而不是等待输入。
- 会话发现：读取上述两个根目录中的 `conversation_summaries.db`，只查询 `conversation_summaries` 表的 `conversation_id`、`title`、`workspace_uris`、`last_modified_time`；将 JSON 列表中首个本机 `file://` URI 解码为目录。同 ID 跨库重复时保留更新时间较新的元数据及其来源判断。
- 续接判断：CLI 根目录为 `local-store`、`resumable=true`；IDE 根目录为 `desktop-store`、`resumable=false`、`resume_command=null`。实测两套存储独立，IDE ID 在 CLI 存储中不存在，agy 没有导入选项，不能向 IDE ID 提供 `agy --conversation` 命令。
- 导出：在两个根目录的 `conversations/<id>.db` 中定位单个数据库，优先匹配元数据来源；缺少摘要时仍可按 ID 定位。查询 `steps` 的 `idx/step_type/step_format/step_payload`，按 idx 读取，再按消息时间排序。只解析类型 14、15；其他类型不提取文字。
- protobuf 判断依据（本地 IDE 样本）：`step_payload` 是完整未压缩消息，field 1 的 varint 与 `step_type` 相等，field 4 为状态，field 5 为元数据；创建时间在 `5 → 1 → {1: seconds, 2: nanos}`。6 个类型 14 步骤的用户输入位于 length-delimited `19 → 2`；`19 → 3` 同时含重复文字块、图片/上下文结构，不能拼接全部字符串。176 个类型 15 中仅 6 个含回复文字 `20 → 1`，且各自与 `20 → 8` 完全相同，故只取 field 1 一次；`20 → 3` 为思考文字，`20 → 7` 是带工具名/JSON 参数的重复结构，均不导出。有回复与工具并存时仍只保留回复字段。
- 排除依据：类型 132（191 行）在 `140` 分支带调用与执行结果；101（6 行）的 `114 → 3` 均为 `task_notification`，其余字段带任务通知与步骤/文件引用；23（1 行）的 `30` 分支含会话压缩摘要、引用和用户输入副本，均不能视为新的用户/Agent 回复。实现按明确的 field/wire 路径解析，不搜索可打印字符串，不把思考、工具输出或摘要当正文。仅用标准库实现带边界检查的 protobuf varint/length-delimited（以及跳过 fixed32/fixed64）解码；未知字段跳过，已知消息损坏时报错。
- 稳定性：内部 SQLite 格式。以 `file:<path>?immutable=1` URI 打开，不创建锁或 sidecar；只读已 checkpoint 的主库快照，WAL 中尚未 checkpoint 的新增/改名可能暂不可见，不执行 checkpoint。时间兼容 ISO 8601 与 Unix 秒/毫秒。
- 限制：当前正文适配 `step_format=0` 与上述字段布局；CLI 导出使用同一布局并有假数据覆盖，本机 CLI 摘要对应的正文文件缺失，尚无真实 CLI 正文验证。未来的新步骤类型默认忽略，字段变化可能需要更新适配。

官方参考：[Antigravity Headless Mode](https://antigravity.google/docs/cli/headless/)。

## Codex

- 发现：`codex`。
- 新建：`codex exec --cd <workspace> --json --skip-git-repo-check ... -`，prompt 从 stdin 传入。
- read：新会话使用 `--sandbox read-only`；续接使用 `-c sandbox_mode="read-only"`。
- write：`--dangerously-bypass-approvals-and-sandbox`。
- 续接：`codex exec resume --json ... <thread_id> -`，read 续接保留只读沙箱，write 续接使用最高权限。
- 解析：JSONL 中的 `thread.started`、`item.completed` agent message、`turn.failed` 和 `error`。
- 认证：沿用 Codex CLI 默认配置与登录，不复制其他目录的 provider 设置。
- 会话发现：`local-store`。逐行读取 `$CODEX_HOME/session_index.jsonl` 的 `id`、`thread_name`、`updated_at`；跳过坏行后，同 ID 以文件中最后一条有效记录为准，先去重再匹配标题。只为过滤、排序和限制后返回的会话定位 `sessions/**/rollout-*-<id>.jsonl`，只解析第一行 `session_meta.payload.cwd`；找不到 rollout 时目录为 `null`。
- 稳定性：内部追加式索引，可能随版本变化；不从对话正文恢复缺失标题，也不读取归档或其他数据根目录。
- 已知限制：不读取或解压 `archived_sessions` 中的 `.jsonl.zst`；已归档或会话文件已删除的会话仍可由索引发现，其 `workspace` 为 `null`。

官方参考：[Codex CLI](https://github.com/openai/codex)。

## OpenCode

- 发现：`opencode`，也可用 `OIL_DELEGATE_OPENCODE_BIN` 指向受信任 sidecar。
- 新建：`opencode run --format json --dir <workspace> <prompt>`；续接增加 `--session <session_id>`。
- read：增加 `--agent plan`，并设置进程级 `OPENCODE_PERMISSION`，禁止 edit、bash、external_directory 和 question。
- write：进程级权限允许 edit、bash 和 external_directory；question 仍拒绝，避免无交互任务等待用户。该方式不依赖新版才有的 `--auto`，同时适用于 1.3.9。
- 解析：事件及其 `part` 中的 `sessionID/sessionId/session_id`；连接 text 事件的 `part.text`，读取 error 事件。
- 认证：使用 `opencode auth login` 或其现有 provider 配置；包装器不读取认证文件。
- 会话发现：`unsupported`。本机未安装可供离线核实的 OpenCode CLI，未验证官方列表命令的版本、参数和元数据字段。稳定性：未接入；不猜测命令或本地数据库格式。

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
- 会话发现：`unsupported`。本机 launcher 与 headless 参数中未发现独立历史列表命令；内部 session-query 服务不视为稳定的官方 CLI。稳定性：未接入，不加载 profile 或访问对话存储来获取标题。

官方参考：[DSH Headless](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md)。

## Grok Build

- 发现：`grok`，Runner 名为 `grok`，接受 `grok-build`。
- 新建：`grok --cwd <workspace> -p <prompt> --output-format json`；续接增加 `--resume <sessionId>`。`--session-id` 只用于新建指定 UUID，不用于续接。
- read：`--permission-mode plan`。
- write：`--always-approve`，自动批准编辑和命令；deny rules、hooks 和 admin locks 仍然有效。
- 解析：最终 JSON 的 `sessionId`、`text`、`stopReason`；错误对象使用 `type/message`。
- 认证：复用 Grok 已有登录；无浏览器环境由用户通过官方 device-auth 或 XAI 凭据链路另行配置。
- 会话发现：`unsupported`。本机 1.0.13 有 `grok sessions list/search`，但帮助中无 JSON 元数据出口；`search` 明确搜索 summaries 和 first prompts，不能等同于仅按标题搜索。尚未确认完整、纯元数据的稳定列表格式，因此不解析展示文本或读取 transcript。稳定性：未接入，等待可验证的元数据接口。

官方参考：[Grok Build Headless Mode](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md)。

## Kimi Code

- 发现：`kimi`，Runner 名为 `kimi`，接受 `kimi-code`。
- 新建：`kimi -p <prompt> --output-format stream-json`；续接增加 `-S <session_id>`。
- write：`-p` 自身已经使用 auto 权限，不再添加 `--auto`。官方明确禁止 `-p` 与 `--auto`、`--yolo` 或 `--plan` 同用。
- read：不支持。非交互模式自带 auto 权限，无法保证只读；收到 read 请求时明确拒绝，不伪装成 plan。
- 解析：assistant 的文本 content 和 `session.resume_hint.session_id`；同时兼容事件中的 session 字段与 error。
- 认证：复用 Kimi Code 已有登录；静态 deny 规则仍然有效，登录由官方 CLI 另行完成。
- 会话发现：`official`。调用 `kimi session list --all --json`，只取 `id`、`title`、`workDir`、`updatedAt`（毫秒时间戳字符串）；在包装器中统一筛选标题、排序和限制。官方数据目录覆盖变量是 `KIMI_CODE_HOME`，仍支持统一的二进制路径覆盖。不使用 `show/export` 或模型调用。
- 稳定性：优先依赖官方 JSON 列表，整体列表无法解析或命令失败计入 `errors.kimi`；单行无效仅计入 `skipped.kimi`，不回退到内部数据库或对话记录。

官方参考：[Kimi Command](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-command.html)。

## 失败处理

- **找不到命令**：安装目标 CLI，或通过 `config --set-bin <runner>=<path>` 指定真实可执行文件；不自动启用其他 Runner。
- **未认证/额度不足**：停止并报告目标 CLI 的官方修复入口，不读取或打印凭据。最高权限不能解决登录或额度问题。
- **权限拒绝**：先检查静态 deny、hooks、管理锁和实际 mode。显式 read 不自动升级为 write；默认 write 也不绕过目标产品的强制限制。
- **输出无法解析**：同时保留协议错误、stderr、原始 stdout 和退出码；先确认实际 CLI 版本与输出格式，不把空响应当成功。
- **超时**：保留部分事件与结果，清理进程后不盲目重试。先缩小任务或确认是否卡在认证、权限或网络。
- **续接失败**：核对 Runner、session ID、cwd 和会话归属；跨 Runner 必须新建任务。
