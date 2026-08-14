# SignalFeed

SignalFeed 从 OpenAI、Anthropic、DeepSeek、Kimi、GLM 和 MiniMax 的 14 个高信号官方入口发现新内容，通过确定性 Collector、跨来源精确去重和按信源配置的 Jina Reader 提取正文，再用一个 Responses 兼容模型生成简体中文标题和 3–5 条完整要点。每篇文章独立发送一条飞书富文本消息并立即记录送达，单篇失败不会阻塞其他文章。

## 项目文档

- [产品愿景与路线图](docs/product-vision.md)
- [六厂商多信源技术规格](docs/multi-source-specification.md)
- [单一官方信源闭环技术规格（历史基线）](docs/single-source-specification.md)
- [从零部署与运行验收](docs/deploy-from-scratch.md)

## 准备

需要 [uv](https://docs.astral.sh/uv/) 和 CPython 3.14：

```bash
uv python install 3.14
uv sync --locked
```

以仅当前用户可读写的权限创建模型配置，并填写一个真实的 Responses 兼容模型和 HTTPS API 端点：

```bash
install -m 0600 models.example.toml models.toml
```

`models.toml` 必须且只能包含一个模型，以及 `model`、`protocol`、`base_url`、`api_key_env` 四个字段。`protocol` 固定为 `openai_responses`，`api_key_env` 固定为 `SIGNALFEED_LLM_API_KEY`。本地 `models.toml` 已被 Git 忽略。

在项目根目录创建权限为 `0600` 的 `.env`，再替换其中的占位值。进程中已存在的同名环境变量优先，文件不会覆盖它们：

```bash
install -m 0600 .env.example .env
```

`SIGNALFEED_LLM_API_KEY` 为模型密钥。`--send` 还需要以下四项飞书企业自建应用配置：

- `FEISHU_APP_ID`：应用凭证中的 App ID；
- `FEISHU_APP_SECRET`：应用凭证中的 App Secret；
- `FEISHU_RECEIVE_ID_TYPE`：接收者 ID 类型，允许 `chat_id`、`open_id`、`union_id`、`user_id` 或 `email`；
- `FEISHU_RECEIVE_ID`：与上述类型对应的群聊或用户 ID。

`SIGNALFEED_DB_PATH` 默认示例使用独立的中文简报数据库。

`.env`、`models.toml` 和 `data/` 都不会被 Git 跟踪。应用错误不会回显模型 API Key、App Secret、接收者 ID 或带凭据的 URL。

信源、检查窗口、网络限制、匹配字段、关键词和飞书消息大小位于 `config.toml`。每个 `[[sources]]` 都包含稳定唯一的 `name`、`url`、`collector`、`transport`、`content_mode`、`window_size`、`allowed_hosts` 和布尔 `filter`。`filter = true` 才应用全局 `[filter]`；匹配忽略大小写，并为英文关键词使用 ASCII 单词边界。支持的 Collector 固定为 `rss`、`markdown_index`、`markdown_changelog`、`markdown_cards` 和 `next_data_index`，列表解析不使用 LLM。

旧 `[source]` 配置仍会映射为单个 RSS 原文来源。信源名称同时是 SQLite 持久化身份，建立状态后不要随意改名；原有 `OpenAI News` 名称和送达记录继续生效。完整的 14 个入口与字段约束见[多信源技术规格](docs/multi-source-specification.md)。

## 安全预览

```bash
uv run --locked python -m signalfeed --dry-run
```

dry-run 会真实访问配置的官方入口；遇到已有基线之外的新文章时，也会按需访问 Jina Reader 和模型 API，并按发送顺序预览每篇飞书逻辑消息。它不要求飞书应用配置，不会访问飞书 OpenAPI，也不会输出凭证或接收者。若来源尚未初始化，dry-run 只显示基线预览；无论数据库是否存在，都不会创建目录、数据库、表或记录。已有数据库中的送达状态和中文缓存只读复用。

Jina Reader 按当前信源接收域名白名单，只允许无凭证的 HTTPS URL，以及白名单主机本身或其子域。客户端超时为 45 秒、响应上限为 1 MiB、正文上限为 6000 tokens；列表页保留链接但移除图片，文章正文继续移除图片、裸 URL 和链接目标。Kimi 官方页面发现的官方 GitHub/Hugging Face 项目可以作为文章目标，但不会扩展为两个站点的主动信源。

模型调用使用官方 OpenAI Python SDK 的异步客户端，超时 60 秒、关闭 SDK 重试、设置 `store=false`，单次输出上限为 8192 tokens。摘要可以并行生成，输出会在本地严格验证为一个中文标题和 3–5 条完整中文要点；发送顺序仍按配置中的来源顺序和各官方列表顺序。

Feed 中的完整发布时间会转换为北京时间；页面只提供日期或月份时保留该精度，不伪造具体时分秒或日期。每篇消息标题为 `YYYY-MM-DD · SignalFeed · {来源}`，中文新闻标题直接显示为蓝色原文链接，不额外显示“查看原文”行。程序不会在本地截断模型生成的标题或摘要，也不会追加省略号。

可通过 `--config PATH` 和 `--models-config PATH` 分别覆盖业务配置与模型配置。

## 发送

先在飞书开放平台创建企业自建应用，然后完成以下配置：

1. 为应用启用机器人能力；
2. 申请应用身份权限 `im:message:send_as_bot`；
3. 配置应用可用范围，创建版本并发布；
4. 若通过 `chat_id` 向群聊发送，将机器人加入目标群；
5. 将 App ID、App Secret、接收者 ID 类型和接收者 ID 写入 `.env`。

飞书接口细节见[发送消息接口](https://open.feishu.cn/document/server-docs/im-v1/message/create?lang=zh-CN)和[富文本结构](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/im-v1/message/create_json)。

```bash
uv run --locked python -m signalfeed --send
```

`--send` 会在采集前完整校验四项飞书环境变量，缺失或接收者类型非法时立即失败。SDK 使用 App ID/App Secret 获取并管理 `tenant_access_token`。空数据库首次运行时，所有成功采集的来源只在 SQLite 中建立窗口基线，不补发历史内容；以后新增的来源也各自原子建立基线。旧数据库若已有 `OpenAI News` 送达记录，会自动把该来源视为已初始化。

每篇新增文章依次完成正文取得、中文摘要、单篇消息构建、发送和送达记账。摘要成功后立即缓存；消息必须完整装入默认 28 KiB 上限，为飞书 30 KiB 富文本限制保留约 2 KiB OpenAPI 封装余量；发送成功后立即记录该篇送达。正文、摘要、超大消息、构建或发送失败都只影响当前文章，失败项保留下轮重试，后续文章继续处理。

来源或文章失败时，程序会向同一飞书目标最佳努力发送包含来源、文章名和失败阶段的小型告警。告警成功后，同一故障事件在恢复前不重复提醒；文章成功送达或来源恢复采集会清除活动故障，但不发送恢复通知。告警自身失败不会递归告警，并会在下轮继续尝试。

本轮只要存在任意来源或文章失败，CLI 会在处理其他文章后返回 `1`；全部成功、只建立基线或没有新增内容时返回 `0`。结束时会输出发送、失败、基线和跳过数量汇总。

## 自动运行与部署

生产环境继续使用上述一次性 CLI，由 `signalfeed.timer` 在北京时间每天运行，包括周末和节假日：

| 时间 | 用途 |
| --- | --- |
| `09:00` | A 股与港股上午连续交易开始前半小时 |
| `12:30` | A 股与港股下午连续交易开始前半小时 |
| `21:00` | 晚间简报 |

交易时段参考[上交所交易规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml)与[港交所交易时间说明](https://www.hkex.com.hk/Global/Exchange/FAQ/Securities-Market/Trading?sc_lang=en)。timer 使用 `Asia/Shanghai` 时区和 `Persistent=true`，服务器停机错过计划后会在恢复时补跑一次。

PR 与 `main` push 都会运行 CI；只有本仓库 `main` 的成功 push CI 会部署对应 commit。仓库需要四个 Actions Secrets：`DEPLOY_HOST`、`DEPLOY_USER`、`DEPLOY_SSH_KEY`、`DEPLOY_PATH`。部署会在共享锁内确认该 commit 仍是远端 `main` 最新版本，再更新代码和锁定依赖；过时 workflow 会被跳过。部署完成后只重启并检查 timer，不会立即发送简报。

service 和 CD 共用 `data/signalfeed.lock`，避免任务重叠或在运行中更新代码。service 最多等待锁 30 分钟，实际运行也限制为 30 分钟。日志通过以下命令查看：

```bash
journalctl -u signalfeed.service
```

仓库中的 `deploy/systemd/` 是管理员安装模板。日常 CD 不会覆盖 `/etc/systemd/system`；只有首次初始化或明确更新部署配置时才应按[从零部署说明](docs/deploy-from-scratch.md)重新渲染并安装单元文件。多信源版本沿用原调度和接收目标；内容故障由飞书一次性告警补充可见性，systemd 状态和 journald 仍用于进程级巡检。

## 测试

```bash
uv run --locked python -m unittest discover -s tests -v
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python -m compileall -q signalfeed tests
```

## 当前非目标

- 不接入 Codex、Claude Code、Kimi Code 或 MiniMax Agent 的逐版本补丁流水；
- 不做标题相似度、语义去重、排序评分或 GitHub/Hugging Face 主动发现；
- 不新增调度时间、模型协议、飞书目标、额外失败重试或 OpenAPI `uuid` 去重；
- 每次运行仍只向一个配置目标发送，不接收飞书事件；部署与定时运行通过文件锁串行化；
- 飞书成功与本地 SQLite 提交无法形成跨系统原子事务，极端情况下可能重复发送一次。
