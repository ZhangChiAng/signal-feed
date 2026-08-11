# SignalFeed

SignalFeed 从 OpenAI 官方 RSS 的最新 20 条内容中筛选关键词，通过 Jina Reader 提取文章正文，再用一个 Responses 兼容模型生成简体中文标题和 3–5 条完整要点，最后把尚未成功送达的条目按大小自动拆成多条飞书富文本简报。

## 项目文档

- [产品愿景与路线图](docs/product-vision.md)
- [单一官方信源闭环技术规格](docs/single-source-specification.md)

## 准备

需要 [uv](https://docs.astral.sh/uv/) 和 CPython 3.14：

```bash
uv python install 3.14
uv sync --locked
```

复制模型配置示例，并填写一个真实的 Responses 兼容模型和 HTTPS API 端点：

```bash
cp models.example.toml models.toml
```

`models.toml` 必须且只能包含一个模型，以及 `model`、`protocol`、`base_url`、`api_key_env` 四个字段。`protocol` 固定为 `openai_responses`，`api_key_env` 固定为 `SIGNALFEED_LLM_API_KEY`。本地 `models.toml` 已被 Git 忽略。

复制环境变量示例，在项目根目录创建权限为 `0600` 的 `.env`，再替换其中的占位值。进程中已存在的同名环境变量优先，文件不会覆盖它们：

```bash
cp .env.example .env
chmod 600 .env
```

`SIGNALFEED_LLM_API_KEY` 为模型密钥；`FEISHU_WEBHOOK_URL` 仅在 `--send` 时必需；`SIGNALFEED_DB_PATH` 默认示例使用独立的中文简报数据库。

`.env`、`models.toml` 和 `data/` 都不会被 Git 跟踪。应用错误不会回显 API Key、Webhook 或带凭据的 URL。

信源、检查窗口、RSS 网络限制、匹配字段、关键词和飞书消息大小位于 `config.toml`。默认同时检查 `title` 与 `content`；匹配忽略大小写，并为英文关键词使用 ASCII 单词边界。

## 安全预览

```bash
uv run --locked python -m signalfeed --dry-run
```

dry-run 会真实访问 OpenAI RSS、Jina Reader 和模型 API，并按发送顺序每行打印一条完整的飞书 JSON；它不会访问飞书 Webhook，也不会创建或修改 SQLite。若数据库已存在，预览会读取送达状态和命中的中文缓存，但不会写入任何内容。

Jina Reader 只接受 RSS 中的 HTTPS `openai.com` 文章，客户端超时为 45 秒、响应上限为 1 MiB、正文上限为 6000 tokens，并移除图片和正文链接。模型调用使用官方 OpenAI Python SDK 的异步客户端，超时 60 秒、关闭 SDK 重试、设置 `store=false`，单次输出上限为 8192 tokens。模型请求并行执行，并以信号量将“请求发出后到响应完成前”的并发数硬限制为 500；Jina 正文读取仍保持单路。输出会在本地再次严格验证为一个中文标题和 3–5 条完整中文要点，最终消息顺序仍与 RSS 顺序一致。

RSS 发布时间会统一转换为北京时间。每次运行开始时生成一次北京时间日期，所有消息使用共同标题 `YYYY-MM-DD · SignalFeed`，其中 `SignalFeed` 用于满足机器人安全关键词；每篇中文新闻标题直接显示为蓝色原文链接，不再额外显示“查看原文”行，发布时间显示为 `北京时间：YYYY-MM-DD HH:MM:SS`。程序不会在本地截断模型生成的标题或摘要，也不会追加省略号。

可通过 `--config PATH` 和 `--models-config PATH` 分别覆盖业务配置与模型配置。

## 发送

```bash
uv run --locked python -m signalfeed --send
```

`--send` 缺少 `FEISHU_WEBHOOK_URL` 时会立即失败。中文生成成功后立即写入缓存；所有消息会在发送前完成大小预检，单篇完整摘要若无法装入一条消息，本轮不会发送任何内容。某个正文或模型调用失败时，整轮也不会发送，已成功生成的中文缓存会在下次复用，不会退回英文摘要。

通过预检后，程序会按 RSS 顺序贪心拆分并顺序发送所有不超过 18 KiB 的消息。每批收到飞书业务响应 `code = 0` 后立即记录该批送达；若中途失败则停止，下一次运行只补发尚未成功的条目。全部条目成功送达后，再次运行应输出：

```text
No new matching items.
```

## 测试

```bash
uv run --locked python -m unittest discover -s tests -v
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python -m compileall -q signalfeed tests
```

## 当前非目标

- 只支持 OpenAI RSS、最新 20 条、单进程运行和手动 CLI；模型请求并发上限固定为 500；
- Jina Reader 是当前允许的第三方正文提取服务；
- 没有启用飞书签名校验，也不支持重叠运行；
- 飞书成功与本地 SQLite 提交无法形成跨系统原子事务，极端情况下可能重复发送一次。
