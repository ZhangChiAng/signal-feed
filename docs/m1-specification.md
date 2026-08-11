# SignalFeed M1 技术规格

> 文档状态：工程实现完成，外部发送待验收  
> 最近更新：2026-08-11

## 1. 目标与状态

M1 是 SignalFeed 的最小端到端竖切，用于验证以下能力：

> 从一个真实的大模型厂商官方信息源发现新内容，完成标准化、关键词过滤和持久化去重，并生成或发送飞书消息。

当前实现使用 OpenAI 官方 RSS 作为唯一信源。采集、过滤、去重、消息构建、飞书请求、安全预览和命令行入口均已完成，20 项自动化测试通过。

当前环境没有配置 `FEISHU_WEBHOOK_URL`，因此尚未执行真实飞书群发送。M1 的工程实现已完成，但上线验收需要提供有效 Webhook 后再执行一次发送及重复运行验证。

## 2. 范围

### 已实现

- OpenAI 官方 RSS 采集；
- RSS 条目解析、HTML 清理和 UTC 时间标准化；
- 基于配置的标题与正文关键词过滤；
- 基于来源、稳定 ID 和 URL 的 SQLite 去重；
- 飞书富文本简报构建和 Webhook 发送；
- 不访问 Webhook、不修改数据库的 dry-run；
- 网络超时、响应大小限制、坏条目跳过和错误报告；
- 单次手动运行的 CLI 与自动化测试。

### 不在 M1 范围内

- 其他厂商、GitHub、Hugging Face、Hacker News、X 或知乎 Collector；
- Web 前端、用户系统或阅读器；
- LLM、Embedding、自动摘要或推荐系统；
- 自动调度、多进程并发或分布式部署；
- 跨来源标题相似度去重和内容排序。

## 3. 数据流

```text
OpenAI 官方 RSS（最多读取最新 20 条）
        ↓
RSSCollector：下载、解析、清洗与标准化
        ↓
KeywordFilter：检查 title 和 content
        ↓
SQLiteStorage：排除已成功发送的条目
        ↓
build_digest：构建有大小上限的飞书富文本简报
        ↓
dry-run 输出 JSON，或 FeishuNotifier 发送 Webhook
        ↓
发送成功后记录 delivered_items
```

执行顺序保证只有飞书业务响应 `code = 0` 后，本轮实际装入简报的条目才会写入去重数据库。发送失败的条目不会被记录，下次运行会继续重试。

## 4. 领域模型

Collector 输出统一的不可变 `NewsItem`：

| 字段 | 含义 |
| --- | --- |
| `source` | 配置中的信源名称 |
| `item_id` | RSS GUID；缺失时使用去除 fragment 的 URL |
| `title` | 清理后的标题 |
| `content` | 清理后的正文或摘要 |
| `url` | 原文 HTTP(S) 地址 |
| `published_at` | 规范化后的 UTC ISO 8601 时间 |
| `author` | 作者；信源未提供时为空字符串 |
| `category` | RSS 分类列表的逗号分隔文本 |
| `guid` | RSS 原始 GUID；未提供时为空字符串 |

所有字段均为字符串，Collector 特有解析逻辑不会渗透到过滤、存储和通知层。

## 5. 组件行为

### 采集与标准化

- 信源 URL、窗口大小和网络限制来自 `config.toml`；
- 当前最多读取 RSS 顺序中的前 20 条，再逐条解析；
- 标题、链接和发布时间缺失或非法的条目会被跳过并记录警告；
- Feed 整体下载失败、响应超限或 XML 非法时，本轮运行失败；
- HTML 摘要会转换为纯文本，发布时间统一转换为 UTC。

### 关键词过滤

- 关键词和匹配字段均由配置提供，业务代码不写死关键词；
- 当前同时检查 `title` 与 `content`，匹配忽略大小写；
- 英文关键词使用 ASCII 单词边界，避免 `API` 误匹配 `rapid`；
- M1 只有“匹配”与“不匹配”两种结果，不进行评分或负向降权。

### 去重与持久化

- 默认数据库为 `data/signalfeed.sqlite3`；
- 以 `(source, item_id)` 为主键，并对 `(source, url)` 建立唯一约束；
- 同一批次内也会按稳定 ID 和 URL 去重；
- dry-run 在数据库存在时只读查询，在数据库不存在时不会创建目录或文件；
- 数据库只记录已成功发送的条目，不保存完整新闻正文或过滤结果。

### 飞书消息

- 每轮最多发送一条富文本简报，按 Feed 顺序优先装入较新的条目；
- 消息包含标题、来源、UTC 发布时间、截断后的摘要和原文链接；
- 编码后的 JSON 不超过配置的消息大小上限；
- 飞书返回非 JSON、响应过大、HTTP 错误或业务 `code` 非零时均视为失败；
- 错误信息不会回显 Webhook URL。

## 6. 配置与运行接口

### 文件配置

`config.toml` 包含四组配置：

| 配置组 | 内容 |
| --- | --- |
| `source` | 信源名称、RSS URL、检查窗口 |
| `network` | 超时、最大响应字节数、User-Agent |
| `filter` | 匹配字段和关键词 |
| `feishu` | 简报标题、消息大小和摘要长度 |

配置缺失、类型错误、非法 URL 或超出飞书大小约束时，程序在发起网络请求前失败。

### 环境变量

| 变量 | 必需性 | 用途 |
| --- | --- | --- |
| `FEISHU_WEBHOOK_URL` | `--send` 必需 | 飞书群自定义机器人 V2 Webhook |
| `SIGNALFEED_DB_PATH` | 可选 | 覆盖默认 SQLite 文件位置 |

Webhook 不写入配置文件或代码仓库。`--send` 缺少 Webhook 时会立即失败，不会隐式降级成预览。

### 命令行

安全预览：

```bash
uv run --locked python -m signalfeed --dry-run
```

真实发送：

```bash
FEISHU_WEBHOOK_URL='https://open.feishu.cn/open-apis/bot/v2/hook/...' \
  uv run --locked python -m signalfeed --send
```

可通过 `--config PATH` 指定其他 TOML 配置。完整的环境准备和运行说明见项目根目录 [README](../README.md)。

## 7. 验收状态

| 验收项 | 状态 | 证据或条件 |
| --- | --- | --- |
| RSS 下载和 `NewsItem` 转换 | 已实现 | Collector 测试覆盖正常、坏条目和异常 Feed |
| 配置化关键词过滤 | 已实现 | 测试覆盖大小写、边界和字段选择 |
| 持久化去重 | 已实现 | 测试覆盖跨实例、同批次和重复 URL |
| 飞书消息生成 | 已实现 | 测试覆盖内容、截断和消息大小 |
| dry-run 无副作用 | 已实现 | 测试验证不发送且不创建数据库 |
| 成功后记录、失败后重试 | 已实现 | 应用编排测试覆盖两种路径 |
| 真实飞书群发送 | 待外部验收 | 需要有效 `FEISHU_WEBHOOK_URL` |
| 连续两次真实运行去重 | 待外部验收 | 首次发送后再次执行 `--send` |

测试命令：

```bash
uv run --locked python -m unittest discover -s tests -v
```

## 8. 已知限制

- 只支持 OpenAI RSS、最新 20 条和单进程串行运行；
- 只提供手动 CLI，没有自动定时任务；
- 每轮最多生成一条简报，超出消息上限的剩余条目留待下轮处理；
- 没有启用飞书签名校验，也不支持重叠运行；
- 飞书成功和 SQLite 提交无法形成跨系统原子事务；若进程恰好在远端成功后、本地提交前终止，可能重复发送一次。

