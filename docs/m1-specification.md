# SignalFeed M1 技术规格

> 文档状态：验收完成
> 最近更新：2026-08-12

## 1. 目标与状态

M1 是 SignalFeed 的最小端到端竖切，用于验证以下能力：

> 从一个真实的大模型厂商官方信息源发现新内容，完成标准化、关键词过滤和持久化去重，并生成或发送飞书消息。

当前实现使用 OpenAI 官方 RSS 作为唯一信源。采集、过滤、去重、消息构建、飞书企业自建应用 OpenAPI 请求、安全预览和命令行入口均已完成，自动化测试通过。

2026-08-12 已使用发布后的企业自建应用机器人完成 OpenAPI 真实群聊发送，并用同一数据库再次运行验证送达去重，企业自建应用迁移验收通过。2026-08-11 的 20 条 RSS 窗口两批发送及去重结果来自已删除的群自定义机器人 Webhook 链路，仅作为历史记录，与本次 OpenAPI 验收分开记录。

## 2. 范围

### 已实现

- OpenAI 官方 RSS 采集；
- RSS 条目解析、HTML 清理和北京时间标准化；
- 基于配置的标题与正文关键词过滤；
- 基于来源、稳定 ID 和 URL 的 SQLite 去重；
- 飞书富文本简报构建和企业自建应用 OpenAPI 发送；
- 不要求飞书配置、不访问飞书、不修改数据库的 dry-run；
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
build_digests：预检并构建一组有大小上限的飞书富文本简报
        ↓
dry-run 逐行输出全部逻辑消息 JSON，或 FeishuNotifier 通过 OpenAPI 顺序发送全部批次
        ↓
每批发送成功后立即记录该批 delivered_items
```

完整批次计划会在任何发送发生前构建并校验。执行顺序保证只有飞书 OpenAPI 响应成功后，该批实际装入简报的条目才会写入去重数据库；中途失败时停止发送，失败批次和后续批次不会被记录，下次运行只补发这些条目。

## 4. 领域模型

Collector 输出统一的不可变 `NewsItem`：

| 字段 | 含义 |
| --- | --- |
| `source` | 配置中的信源名称 |
| `item_id` | RSS GUID；缺失时使用去除 fragment 的 URL |
| `title` | 清理后的标题 |
| `content` | 清理后的正文或摘要 |
| `url` | 原文 HTTP(S) 地址 |
| `published_at` | 规范化后的北京时间 ISO 8601 时间，带 `+08:00` 偏移 |
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
- HTML 摘要会转换为纯文本，发布时间统一转换为 `Asia/Shanghai` 北京时间；无时区 RSS 时间按 UTC 解释后转换。

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

- 每次运行以当日北京时间和产品名组成 `YYYY-MM-DD · SignalFeed`，作为所有批次的共同简报标题，不附加批次编号；
- 新闻标题直接使用飞书富文本链接节点指向原文，不额外显示“查看原文”行；消息包含来源、北京时间和完整模型摘要；
- 不在本地截断中文标题或摘要，也不追加省略号；
- 逻辑消息结构为 `{"msg_type":"post","content":{"zh_cn":...}}`；发送时 `content` 会按 OpenAPI 要求序列化为 JSON 字符串；
- 按 Feed 顺序贪心拆分完整条目，所有编码后的逻辑消息 JSON 均不超过配置的消息大小上限；默认上限 28672 字节，配置最大允许 30720 字节；
- 若任意单篇完整摘要无法装入一条消息，则在发送任何批次前失败；
- SDK 异常或 OpenAPI 业务响应失败时均视为失败；
- SDK 使用 App ID/App Secret 获取并管理 `tenant_access_token`，客户端使用 `network.timeout_seconds`；
- 错误信息不会回显应用凭证或接收者 ID。

## 6. 配置与运行接口

### 文件配置

`config.toml` 包含四组配置：

| 配置组 | 内容 |
| --- | --- |
| `source` | 信源名称、RSS URL、检查窗口 |
| `network` | 超时、最大响应字节数、User-Agent |
| `filter` | 匹配字段和关键词 |
| `feishu` | 单条消息的最大字节数 |

旧外部配置中的 `feishu.title` 和 `feishu.summary_max_chars` 仍可存在，但程序不再读取或使用这两个字段。`max_payload_bytes` 不得超过 30720；仓库默认值为 28672，为官方 30 KiB 富文本限制保留约 2 KiB OpenAPI 封装余量。

配置缺失、类型错误、非法 URL 或超出飞书大小约束时，程序在发起网络请求前失败。

### 环境变量

| 变量 | 必需性 | 用途 |
| --- | --- | --- |
| `FEISHU_APP_ID` | `--send` 必需 | 企业自建应用 App ID |
| `FEISHU_APP_SECRET` | `--send` 必需 | 企业自建应用 App Secret |
| `FEISHU_RECEIVE_ID_TYPE` | `--send` 必需 | `chat_id`、`open_id`、`union_id`、`user_id` 或 `email` |
| `FEISHU_RECEIVE_ID` | `--send` 必需 | 与 ID 类型对应的单个接收目标 |
| `SIGNALFEED_LLM_API_KEY` | 始终必需 | Responses 兼容模型密钥 |
| `SIGNALFEED_DB_PATH` | 可选 | 覆盖默认 SQLite 文件位置 |

应用凭证不写入配置文件或代码仓库。`--send` 会聚合报告缺少的飞书变量，接收者类型仅允许白名单值；校验失败不会回显配置值，也不会隐式降级成预览。dry-run 不要求这四项飞书配置，输出中也不包含凭证或接收者。

发送前需在飞书开放平台创建企业自建应用、启用机器人、申请应用身份权限 `im:message:send_as_bot`、配置可用范围并创建版本发布。向群聊发送时还需将机器人加入目标群。接口与消息格式分别见[发送消息接口](https://open.feishu.cn/document/server-docs/im-v1/message/create?lang=zh-CN)和[富文本结构](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/im-v1/message/create_json)。

### 命令行

安全预览：

```bash
uv run --locked python -m signalfeed --dry-run
```

真实发送：

```bash
uv run --locked python -m signalfeed --send
```

可通过 `--config PATH` 指定其他 TOML 配置。完整的环境准备和运行说明见项目根目录 [README](../README.md)。

## 7. 验收状态

| 验收项 | 状态 | 证据或条件 |
| --- | --- | --- |
| RSS 下载和 `NewsItem` 转换 | 已实现 | Collector 测试覆盖正常、坏条目和异常 Feed |
| 配置化关键词过滤 | 已实现 | 测试覆盖大小写、边界和字段选择 |
| 持久化去重 | 已实现 | 测试覆盖跨实例、同批次和重复 URL |
| 飞书消息生成 | 已实现 | 测试覆盖完整内容、原文标题链接、北京时间、自动分批和消息大小 |
| dry-run 无副作用 | 已实现 | 测试验证不发送且不创建数据库 |
| 成功后记录、失败后重试 | 已实现 | 应用编排测试覆盖两种路径 |
| 企业自建应用 OpenAPI 真实群发送 | 已验收 | 已发布应用机器人成功向目标群发送富文本简报 |
| 新链路连续两次真实运行去重 | 已验收 | 首次成功送达后使用同一数据库再次执行，输出 `No new matching items.` |

上述两项证据来自 2026-08-12 的企业自建应用 OpenAPI 实测。历史 Webhook 验收曾成功发送 16 条和 4 条并验证去重，但该路径已移除，不作为新链路验收证据。

测试命令：

```bash
uv run --locked python -m unittest discover -s tests -v
```

## 8. 已知限制

- 只支持 OpenAI RSS、最新 20 条和单进程运行；中文摘要阶段的模型请求并发上限为 500；
- 只提供手动 CLI，没有自动定时任务；
- 每次运行只配置一个接收目标，不接收飞书事件，也不支持重叠运行；
- 不提供额外 OpenAPI 重试或 `uuid` 去重；
- 飞书成功和 SQLite 提交无法形成跨系统原子事务；若进程恰好在远端成功后、本地提交前终止，可能重复发送一次。
