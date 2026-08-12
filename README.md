# SignalFeed

SignalFeed 从 OpenAI 官方 RSS 的最新 20 条内容中筛选关键词，通过 Jina Reader 提取文章正文，再用一个 Responses 兼容模型生成简体中文标题和 3–5 条完整要点，最后把尚未成功送达的条目按大小自动拆成多条飞书富文本简报。

## 项目文档

- [产品愿景与路线图](docs/product-vision.md)
- [单一官方信源闭环技术规格](docs/single-source-specification.md)
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

信源、检查窗口、RSS 网络限制、匹配字段、关键词和飞书消息大小位于 `config.toml`。默认同时检查 `title` 与 `content`；匹配忽略大小写，并为英文关键词使用 ASCII 单词边界。

## 安全预览

```bash
uv run --locked python -m signalfeed --dry-run
```

dry-run 会真实访问 OpenAI RSS、Jina Reader 和模型 API，并按发送顺序每行打印一条完整的飞书逻辑消息 JSON；它不要求飞书应用配置，不会访问飞书 OpenAPI，也不会输出凭证或接收者，不会创建或修改 SQLite。若数据库已存在，预览会读取送达状态和命中的中文缓存，但不会写入任何内容。

Jina Reader 只接受 RSS 中的 HTTPS `openai.com` 文章，客户端超时为 45 秒、响应上限为 1 MiB、正文上限为 6000 tokens，并移除图片和正文链接。模型调用使用官方 OpenAI Python SDK 的异步客户端，超时 60 秒、关闭 SDK 重试、设置 `store=false`，单次输出上限为 8192 tokens。模型请求并行执行，并以信号量将“请求发出后到响应完成前”的并发数硬限制为 500；Jina 正文读取仍保持单路。输出会在本地再次严格验证为一个中文标题和 3–5 条完整中文要点，最终消息顺序仍与 RSS 顺序一致。

RSS 发布时间会统一转换为北京时间。每次运行开始时生成一次北京时间日期，所有消息使用共同标题 `YYYY-MM-DD · SignalFeed`；每篇中文新闻标题直接显示为蓝色原文链接，不再额外显示“查看原文”行，发布时间显示为 `北京时间：YYYY-MM-DD HH:MM:SS`。程序不会在本地截断模型生成的标题或摘要，也不会追加省略号。

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

`--send` 会在采集前完整校验四项飞书环境变量，缺失或接收者类型非法时立即失败。SDK 使用 App ID/App Secret 获取并管理 `tenant_access_token`。中文生成成功后立即写入缓存；所有消息会在发送前完成大小预检，单篇完整摘要若无法装入一条消息，本轮不会发送任何内容。某个正文或模型调用失败时，整轮也不会发送，已成功生成的中文缓存会在下次复用，不会退回英文摘要。

通过预检后，程序会按 RSS 顺序贪心拆分并顺序发送所有不超过默认 28 KiB 的逻辑消息，为飞书 30 KiB 富文本限制保留约 2 KiB 的 OpenAPI 封装余量。每批收到成功的 OpenAPI 业务响应后立即记录该批送达；若中途失败则停止，下一次运行只补发尚未成功的条目。全部条目成功送达后，再次运行应输出：

```text
No new matching items.
```

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

仓库中的 `deploy/systemd/` 是管理员安装模板。日常 CD 不会覆盖 `/etc/systemd/system`；只有首次初始化或明确更新部署配置时才应按[从零部署说明](docs/deploy-from-scratch.md)重新渲染并安装单元文件。首版没有主动失败告警，启用后需人工完成 7 天、21 个计划时点的观察。

## 测试

```bash
uv run --locked python -m unittest discover -s tests -v
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python -m compileall -q signalfeed tests
```

## 当前非目标

- 只支持 OpenAI RSS、最新 20 条和单进程一次性 CLI；生产调度依赖 systemd，模型请求并发上限固定为 500；
- Jina Reader 是当前允许的第三方正文提取服务；
- 每次运行只向一个配置目标发送，不接收飞书事件；部署与定时运行通过文件锁串行化；
- 不增加额外发送重试或 OpenAPI `uuid` 去重；
- 不提供主动失败告警，当前依赖 systemd 状态和 journald 人工巡检；
- 飞书成功与本地 SQLite 提交无法形成跨系统原子事务，极端情况下可能重复发送一次。
