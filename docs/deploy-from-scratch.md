# SignalFeed 从零部署

本文说明如何把 SignalFeed 安装为一次性 systemd 服务，并由 timer 在北京时间每天 `08:30`、`12:30`、`21:00` 运行（早间档在 DeepSeek 09:00 起的峰段定价开始前完成模型调用）。应用会按配置顺序处理 OpenAI、Anthropic、DeepSeek、Kimi、GLM 和 MiniMax 的多个官方信源。日常发布由 GitHub Actions 更新已经通过 CI 的 commit；systemd 单元只在首次安装或明确修改部署配置时由管理员重新安装。

## 1. 准备部署账户和目录

服务器需要 Git、`flock`、systemd、uv 和 CPython 3.14。SSH 部署账户与 systemd 服务应使用同一个非 root 用户，以下示例使用已有账户 `deploy` 和绝对路径 `/srv/signal-feed`。部署路径和用户名应只包含字母、数字、点、下划线、连字符与斜杠，以便安全渲染模板。

管理员先创建并授权目录：

```bash
sudo install -d -o deploy -g deploy -m 0755 /srv/signal-feed
```

随后用部署账户克隆仓库并同步锁定依赖：

```bash
git clone https://github.com/ZhangChiAng/signal-feed.git /srv/signal-feed
cd /srv/signal-feed
uv python install 3.14
uv sync --locked
```

## 2. 创建本地配置和持久化目录

在仓库根目录创建仅部署账户可读写的配置文件，再填写真实值：

```bash
install -m 0600 .env.example .env
install -m 0600 models.example.toml models.toml
mkdir -p data
chmod 0700 data
```

`.env`、`models.toml` 与 `data/` 均被 Git 忽略。日常 CD 不运行 `git clean`，不会删除这些服务器本地文件。建议确认数据库环境变量使用 `data/` 下的路径；默认值 `data/signalfeed.sqlite3` 已满足要求。

仓库的 `config.toml` 使用有序 `[[sources]]` 配置。部署前应确认每项都有稳定且唯一的 `name`，以及与入口匹配的 `collector`、`transport`、`content_mode` 和 `allowed_hosts`。信源名称也是 SQLite 持久化标识；建立基线后不应仅为显示效果改名，否则会被视为新信源。

先以部署账户执行安全预览：

```bash
cd /srv/signal-feed
uv run --locked python -m signalfeed --dry-run
```

dry-run 必须成功，并为每个尚未初始化的信源显示首次窗口基线计数。dry-run 可以读取已有去重状态和中文缓存，但不会创建数据库、表或记录。日志中不得出现模型密钥、飞书 App Secret、接收者 ID 或带凭据的 URL。如果任一入口无法下载或解析，其他信源仍应继续预览，但本轮最终返回非零。

## 3. 渲染、验证并安装 systemd 单元

仓库中的单元使用 `@DEPLOY_PATH@` 和 `@SERVICE_USER@` 占位符。管理员在临时目录渲染，先验证再安装：

```bash
deploy_path=/srv/signal-feed
service_user=deploy
unit_tmp_dir="$(mktemp -d)"
trap 'rm -rf "$unit_tmp_dir"' EXIT

sed \
  -e "s|@DEPLOY_PATH@|$deploy_path|g" \
  -e "s|@SERVICE_USER@|$service_user|g" \
  deploy/systemd/signalfeed.service >"$unit_tmp_dir/signalfeed.service"
cp deploy/systemd/signalfeed.timer "$unit_tmp_dir/signalfeed.timer"

sudo systemd-analyze verify \
  "$unit_tmp_dir/signalfeed.service" \
  "$unit_tmp_dir/signalfeed.timer"
sudo install -m 0644 "$unit_tmp_dir/signalfeed.service" /etc/systemd/system/
sudo install -m 0644 "$unit_tmp_dir/signalfeed.timer" /etc/systemd/system/
sudo systemctl daemon-reload
```

确认三个日历表达式的下一次触发时间：

```bash
systemd-analyze calendar --iterations=3 '*-*-* 08:30:00 Asia/Shanghai'
systemd-analyze calendar --iterations=3 '*-*-* 12:30:00 Asia/Shanghai'
systemd-analyze calendar --iterations=3 '*-*-* 21:00:00 Asia/Shanghai'
```

service 通过 `data/signalfeed.lock` 与 CD 互斥：最多等待锁 30 分钟，取得锁后程序最多运行 30 分钟。它以 `UMask=0077` 启动、不能获取新权限，代码和配置只读，仅 `data/` 和隔离的临时目录可写。

## 4. 首次建立基线和启用 timer

dry-run 通过后，先手动运行一次服务。空数据库下，这次运行会分别为所有成功采集的信源原子建立首次窗口基线，不会把现有历史文章发到飞书：

```bash
sudo systemctl start signalfeed.service
sudo systemctl status signalfeed.service --no-pager
sudo journalctl -u signalfeed.service --since today --no-pager
```

检查日志中每个预期信源都出现 `Baseline created`，汇总的基线条目总数符合各来源窗口，且失败数为零。同一轮中一个信源失败不会回滚其他信源已成功的基线；未建立基线的信源会在下次成功采集时重试。

确认日志无凭据后再启用 timer：

```bash
sudo systemctl enable --now signalfeed.timer
sudo systemctl status signalfeed.timer --no-pager
systemctl list-timers signalfeed.timer --all
```

`Persistent=true` 会让服务器从停机中恢复后补跑一次错过的计划，不会逐个补跑所有错过时点。再次手动启动 service 时，若没有新增内容，应正常返回 `0`，并输出包含发送、失败、基线和跳过数量的汇总。

systemd 自身会合并同一个 oneshot service 的并发启动请求。还可在任务运行期间确认另一个进程无法无等待地取得共享锁：

```bash
cd /srv/signal-feed
flock --nonblock data/signalfeed.lock true
```

该命令应返回非零；部署会等待同一把锁，不会在任务执行期间改动虚拟环境或代码。

## 5. 配置 GitHub Actions 自动部署

在 GitHub 仓库 Actions Secrets 中配置：

- `DEPLOY_HOST`：服务器主机名或 IP；
- `DEPLOY_USER`：上述部署账户；
- `DEPLOY_SSH_KEY`：对应私钥；
- `DEPLOY_PATH`：本仓库独立的绝对路径，如 `/srv/signal-feed`。

部署账户还需要以免密 sudo 执行以下三个命令，供非交互式 CD 重启和检查 timer；应按服务器上 `systemctl` 的真实绝对路径配置最小化 sudoers 规则：

```text
systemctl restart signalfeed.timer
systemctl is-active --quiet signalfeed.timer
systemctl status signalfeed.timer --no-pager
```

CI 在 PR 与 `main` push 上执行锁定依赖同步、单元测试、Ruff、格式检查和 compileall。只有本仓库 `main` 的 push CI 成功才会触发 Deploy。部署持锁执行 `git fetch`、远端 `main` SHA 检查、`git reset --hard <verified-sha>` 与 `uv sync --locked`；若 workflow 对应的 SHA 已经过时则直接跳过。最后只重启并检查 `signalfeed.timer`，不会立即运行 service。

日常 CD 不会覆盖 `/etc/systemd/system`。若仓库中的 systemd 模板有意变更，管理员必须重新执行第 3 节的渲染、验证、安装和 `daemon-reload`。

## 6. 运行观察与故障检查

常用检查命令：

```bash
systemctl status signalfeed.timer --no-pager
systemctl status signalfeed.service --no-pager
journalctl -u signalfeed.service
```

任一来源或文章失败都会在其他可处理文章完成后使 `signalfeed.service` 返回非零。每轮结束的汇总必须同时检查发送、失败、基线和跳过四类数量，不能只根据是否有飞书内容判断。已经写入 SQLite 的中文摘要缓存、已成功文章的送达记录和已建立的来源基线会立即保留，供下一次重试继续使用；不要删除或替换 `data/`。

来源下载/解析失败，或单篇正文、摘要、消息构建、大小预检和发送失败时，SignalFeed 会向同一飞书目标最佳努力发送一条小型告警。告警包含来源、文章名和失败阶段；成功告警后，同一故障事件在恢复前保持静默。文章成功送达或来源恢复采集时会清除活动故障，但不发送恢复通知；同类故障以后再次出现时可重新告警。告警自身发送失败不会递归告警，也不会记为已提醒，下轮会再尝试。

首次启用后连续观察 7 天、共 21 个计划时点。每个时点应汇总四类计数，无失败时 service 应为成功状态；同时确认 14 个官方入口能稳定解析，没有重叠执行、异常重复推送、凭据泄露或人工修复。飞书主动告警用于快速发现内容级故障，service 状态和 journald 仍是进程级巡检与排障依据。完成全部 21 个时点后，才能把路线图中的运行验收标记为完成。
