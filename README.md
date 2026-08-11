# SignalFeed

SignalFeed M1 从 OpenAI 官方 RSS 的最新 20 条内容中筛选关键词，将尚未成功发送过的条目合并成一条飞书富文本消息。运行时只使用 Python 标准库。

## 项目文档

- [产品愿景与路线图](docs/product-vision.md)
- [M1 技术规格](docs/m1-specification.md)

## 准备

需要 [uv](https://docs.astral.sh/uv/) 和 CPython 3.14。项目通过 `.python-version` 固定 3.14，并用 `uv.lock` 固定解释器要求；没有第三方运行时依赖。

```bash
uv python install 3.14
uv sync --locked
```

信源、检查窗口、网络限制、匹配字段、关键词和消息大小都在 `config.toml`。默认同时检查 `title` 与 `content`；匹配忽略大小写，并为英文关键词使用 ASCII 单词边界，例如 `API` 不会误命中 `rapid`。

## 安全预览

```bash
uv run --locked python -m signalfeed --dry-run
```

此命令会真实抓取和过滤 RSS，并打印最终飞书 JSON，但不会访问 Webhook，也不会创建或修改 SQLite。因为不写状态，重复预览会再次显示相同内容。若数据库已存在，预览仍会排除已发送条目。

## 发送

在飞书群中创建自定义机器人并取得 V2 Webhook 后运行：

```bash
FEISHU_WEBHOOK_URL='https://open.feishu.cn/open-apis/bot/v2/hook/...' \
  uv run --locked python -m signalfeed --send
```

`--send` 缺少 `FEISHU_WEBHOOK_URL` 时会立即失败，不会隐式降级为预览。默认状态文件为 `data/signalfeed.sqlite3`，可通过 `SIGNALFEED_DB_PATH` 覆盖：

```bash
SIGNALFEED_DB_PATH='/absolute/path/signalfeed.sqlite3' \
FEISHU_WEBHOOK_URL='实际地址' \
  uv run --locked python -m signalfeed --send
```

只有飞书业务响应的 `code` 为 `0` 后，本轮实际装入摘要的条目才会在一个 SQLite 事务中落库。可连续执行两次 `--send` 验证去重：第一次发送，第二次应输出 `No new matching items.`。

## 测试

```bash
uv run --locked python -m unittest discover -s tests -v
```

## M1 限制

- 只支持 OpenAI RSS、最新 20 条、单进程串行运行和手动 CLI。
- 每轮最多发送一个摘要；最新内容优先装入 18 KiB 上限，余下内容下轮再发。
- 没有启用飞书签名校验，也不支持重叠运行。
- 飞书成功与本地 SQLite 提交无法形成跨系统原子事务；进程若恰好在远端成功后、本地提交前崩溃，可能重复发送一次。
