# SignalFeed 六厂商多信源技术规格

> 文档状态：实现与验收中  
> 最近更新：2026-08-14

## 1. 目标与范围

本阶段把单一 `OpenAI News` RSS 闭环扩展为六家模型厂商的 14 个高信号官方入口，同时保留现有模型配置、飞书接收目标和 systemd 调度。核心目标是：

> 每篇新增内容都能独立完成采集、正文提取、中文摘要、飞书发送和送达记账；局部故障可见，但不阻塞其他文章。

本期只做确定性列表解析和精确 URL/条目去重。不使用 LLM 解析列表页，不新增解析依赖，不做标题相似度、语义去重、排序评分或额外重试。

## 2. 官方信源目录

配置顺序既是处理顺序，也是相同规范化 URL 在多个来源出现时的优先级。

| 稳定名称 | 初始入口 | Collector | 正文模式 |
| --- | --- | --- | --- |
| `OpenAI News` | `https://openai.com/news/rss.xml` | `rss` / direct | `article` |
| `OpenAI Developer Blog` | `https://developers.openai.com/blog` | `markdown_index` / jina | `article` |
| `OpenAI API Changelog` | `https://developers.openai.com/api/docs/changelog` | `markdown_changelog` / direct | `inline` |
| `Anthropic Newsroom` | `https://www.anthropic.com/news` | `markdown_index` / jina | `article` |
| `Anthropic Engineering` | `https://www.anthropic.com/engineering` | `markdown_index` / jina | `article` |
| `Claude Platform Release Notes` | `https://platform.claude.com/docs/en/release-notes/overview` | `markdown_changelog` / direct | `inline` |
| `DeepSeek API Change Log` | `https://api-docs.deepseek.com/updates/` | `markdown_changelog` / jina | `inline` |
| `Kimi Research` | `https://www.kimi.com/blog/` | `next_data_index` / direct | `article` |
| `Kimi Open Platform Blog` | `https://platform.kimi.com/blog` | `markdown_index` / jina | `article` |
| `Kimi Open Platform Changelog` | `https://platform.kimi.com/blog/posts/changelog` | `markdown_changelog` / jina | `inline` |
| `GLM Z.AI Model Releases` | `https://docs.z.ai/release-notes/new-released/rss.xml` | `rss` / direct | `inline` |
| `MiniMax News` | `https://www.minimax.io/news` | `markdown_index` / jina | `article` |
| `MiniMax Model Releases` | `https://platform.minimax.io/docs/release-notes/models` | `markdown_cards` / direct | `article` |
| `MiniMax API Releases` | `https://platform.minimax.io/docs/release-notes/apis` | `markdown_changelog` / direct | `inline` |

信源名称是数据库中的持久化身份，不是展示用别名。上线并建立状态后不得随意改名；`OpenAI News` 必须保持原名称，以便旧数据库无损延续送达记录。

Kimi 官方页面中发现的官方 GitHub 或 Hugging Face 项目链接可以成为该条文章的正文目标，但不会触发对 GitHub 或 Hugging Face 的主动发现，也不会把两个站点扩展成新的全站信源。

## 3. 配置接口

业务配置使用有序的 TOML 表数组：

```toml
[[sources]]
name = "Anthropic Engineering"
url = "https://www.anthropic.com/engineering"
collector = "markdown_index"
transport = "jina"
content_mode = "article"
window_size = 20
allowed_hosts = ["anthropic.com"]
filter = false
```

每个信源包含以下字段：

| 字段 | 约束与含义 |
| --- | --- |
| `name` | 非空且在整个数组中唯一；同时作为持久化标识 |
| `url` | 无凭证的 HTTP(S) 官方入口 |
| `collector` | `rss`、`markdown_index`、`markdown_changelog`、`markdown_cards` 或 `next_data_index` |
| `transport` | `direct` 或 `jina`；只决定列表页如何下载 |
| `content_mode` | `article` 抓取目标原文；`inline` 直接使用列表条目正文 |
| `window_size` | 正整数，默认和仓库配置均为 `20` |
| `allowed_hosts` | 非空域名白名单，用于正文目标校验 |
| `filter` | `true` 时应用全局 `[filter]`；`false` 时全部接收 |

旧 `[source]` 配置继续兼容，并映射为单个 `rss`、`direct`、`article` 来源。它保持旧的 OpenAI 正文域名限制和全局过滤行为，便于现有私有部署逐步迁移。

一般 News 和开放平台 Blog 使用全局关键词过滤；Developer Blog、Engineering、Research、模型发布和 API/平台发布记录已经由官方策展，全部接收。全局词表同时包含 Claude、Anthropic、DeepSeek、Kimi、GLM、MiniMax，以及模型、智能体、推理、上下文等中英文关键词；英文匹配继续使用 ASCII 单词边界并忽略大小写。

## 4. 采集契约

五种 Collector 通过注册表按配置枚举选择，统一返回：

```text
CollectionBatch(items, issues)
```

- `items` 是成功标准化的 `NewsItem`，保持官方列表顺序并受 `window_size` 限制；
- `issues` 是可定位到坏条目的 `CollectionIssue`，单个日期、标题、链接或结构异常不会丢弃同源其他条目；
- 整体下载失败、响应超限或页面结构已无法识别时抛出来源级采集失败；
- 列表页由确定性解析器处理，不把页面交给 LLM 推断结构。

`rss` 解析 RSS 条目；`markdown_index` 解析保留链接的 Markdown 卡片/列表；`markdown_changelog` 按日期标题切分滚动更新页；`markdown_cards` 解析带目标链接的 Markdown 卡片；`next_data_index` 从 Next.js 结构化数据读取文章列表。

日期保留页面提供的原始精度：Feed 中的完整时间转换为北京时间；只有日期或月份的页面仍只显示日期或月份，不补造小时、分钟、秒或具体日。

## 5. Jina Reader 与域名边界

Jina 请求按当前信源显式传入 `allowed_hosts`。目标 URL 必须使用 HTTPS、不得带用户名或密码，主机必须精确匹配白名单项或是其子域；`anthropic.com.evil.example` 之类的后缀伪装不会通过。

- Jina 列表页保留 Markdown 链接并移除图片，供确定性 Collector 发现文章目标；
- 文章正文移除图片、裸 URL 和 Markdown 链接目标，只保留可摘要的正文与锚文本；
- 客户端继续使用 45 秒超时、1 MiB 响应上限和 6000 tokens 上限；
- 未显式传入白名单的旧调用仍默认只允许 `openai.com` 及其子域。

`transport` 只约束入口下载方式；`content_mode=article` 的目标正文仍走受限 Reader，`inline` 条目不进行第二次正文抓取。

## 6. 标准化与去重

`NewsItem` 除来源、标题、内容、目标 URL 和原精度日期外，还包含稳定的 `dedupe_key`：

- 普通文章先移除 fragment 和已知跟踪参数，再使用规范化 URL 作为键，实现跨来源精确去重；
- 滚动 Changelog 条目使用来源、原精度日期和规范化内容签名生成键；同一页面、同一天的多个独立条目不会互相覆盖；
- Changelog 内容签名发生变化会形成新的待处理条目；
- 配置靠前的来源优先占用相同规范化 URL，靠后的重复项计入跳过；
- 本期不做标题相似度或 LLM 语义去重。

来源之间按配置顺序处理，来源内维持官方列表顺序。摘要任务可以并行，但结果必须恢复为上述顺序后逐篇发送。

## 7. 首次基线与 SQLite 升级

SQLite 在现有 `delivered_items` 和中文摘要缓存之外增加三组状态：

| 表 | 用途 |
| --- | --- |
| `source_state` | 标记来源是否已经完成首次基线 |
| `baseline_items` | 保存首次窗口中的全部条目，不只保存过滤后条目 |
| `active_failures` | 保存已经成功告警、但对应来源或文章尚未恢复的故障 |

规则如下：

1. 空数据库首次运行时，所有来源只建立基线，不发送历史内容；
2. 新增来源首次成功采集时，在一个事务中写入该来源窗口内全部条目并标记初始化；任一步失败都不留下半份基线；
3. 旧数据库只要已有 `OpenAI News` 的 `delivered_items`，该来源自动视为已初始化，原有去重继续生效；
4. 其他来源各自独立建基线，一个来源失败不影响成功来源提交基线；
5. dry-run 只输出基线预览和计数，不创建数据库、目录、表或任何记录。

建立基线后，只处理不在基线和送达记录中的新 `dedupe_key`；失败文章不会记为已送达，因此下轮仍可重试。

## 8. 逐篇处理与飞书消息

每篇文章是独立工作单元：

```text
正文取得 → 中文摘要 → 单篇消息构建与大小预检 → 飞书发送 → 送达记账
```

- `article` 先抓正文，`inline` 直接使用 Collector 提供的条目内容；
- 每篇生成一个完整简体中文标题和 3–5 条完整要点，保留模型名、产品名、数字与限定条件；
- 成功摘要立即写入缓存，后续发送失败时下轮直接复用；
- 每篇恰好构建一条飞书消息，不再把多篇文章拼成批次；
- 消息标题为 `YYYY-MM-DD · SignalFeed · {来源}`，日期取本轮北京时间；
- 单篇逻辑消息仍必须完整装入默认 28 KiB 上限，不截断标题或摘要；
- 飞书返回成功后立即记录该篇送达，不能等整轮结束再统一提交；
- 正文、摘要、消息构建、超大内容或发送失败都只影响当前文章，后续文章继续处理。

飞书接收目标、企业自建应用鉴权、模型配置、摘要提示词边界和 systemd 每日 `09:00`、`12:30`、`21:00` 调度保持不变。

## 9. 故障告警状态机

来源级失败和文章级失败都向同一飞书目标最佳努力发送一条小型告警，至少包含来源、文章名（来源级故障可使用来源入口）和失败阶段。

```text
首次故障 ──告警成功──> active_failures
   │                         │
   └─告警失败：不记状态       ├─同一事件再次失败：静默
                             └─来源恢复采集或文章成功送达：清除
```

- 同一文章的故障事件以来源和文章身份确定，阶段变化不产生第二条告警；
- 只有告警成功送达后才记录活动故障；告警自身失败不递归告警，下轮继续尝试；
- 文章成功送达后清除其活动故障；来源恢复采集后清除来源级活动故障；
- 不发送恢复通知；清除后若以后再次失败，可重新发送一次告警。

## 10. CLI 结果

运行结束始终输出发送、失败、基线和跳过数量汇总。只要本轮存在任意来源级或文章级失败，程序在完成其他可处理文章后返回 `1`；全部成功、只建立基线或没有新增内容时返回 `0`。

`--dry-run` 不要求飞书应用配置，不访问飞书 OpenAPI；它可以读取既有送达状态和摘要缓存，但对 SQLite 零写入。`--send` 继续在采集前完整校验四项飞书配置。

## 11. 测试与验收

自动化测试至少覆盖：

- 五种 Collector 的固定官方响应夹具、日期精度、窗口、重复链接、坏条目和响应超限；
- `[[sources]]`、唯一名称、枚举值、白名单、旧 `[source]` 兼容和中英文过滤；
- 跨来源规范化 URL 去重、同页多条 Changelog、旧 SQLite 无损升级、原子基线和 dry-run 零写入；
- 正文、摘要、超大消息和发送失败的逐篇隔离，以及成功项立即缓存和记账；
- 首次告警、重复静默、恢复后再告警，以及告警发送失败后的下轮重试；
- 每篇一条消息、完整中文标题、3–5 条要点、顺序发送和最终退出码。

上线前运行完整单元测试、Ruff、格式检查和 `compileall`，并对 14 个真实入口执行联网 dry-run，确认每个来源均能解析且显示预期基线计数。

## 12. 明确非目标

- Codex、Claude Code、Kimi Code、MiniMax Agent 的逐版本补丁流水；
- 新增调度时间、飞书接收目标或模型协议；
- 语义去重、标题相似度、内容评分或跨来源事件聚类；
- GitHub/Hugging Face 主动发现或全站采集；
- 自动失败重试、恢复通知或额外发送幂等协议。
