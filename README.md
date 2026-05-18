# Sentinel v2

通用主题舆情雷达。订阅 Telegram / RSS / Twitter / Reddit / RSSHub 信源，按主题聚合，LLM 自动 triage 发警报 + 出深度报告。

## 架构

![Sentinel v2 architecture](docs/architecture.svg)

数据流（左→右）+ 控制流（顶部橙虚线，触发 service）+ 工具能力区（下方独立）三轨布局。详见 [docs/design.md](docs/design.md) §1。

## 核心功能

### 🛰 多源信源采集 · 6 collector kind 通用接入

按"主题"组织（用户面对的是 *VPN 行业动态*、*AI Coding 工具趋势* 这类业务概念，不是 collector 实现细节）。**同渠道在不同主题下是独立 source 实例**——比如同一个 Telegram 群可同时服务多个 topic，按各自的 keyword 过滤。

| kind | 适合 | 实现 |
|---|---|---|
| `telegram` | 公开频道 / 群组 | telethon · 需要 TELEGRAM_API_ID/HASH |
| `rss` | 普通 RSS + 本机 RSSHub 转出来的社交平台（V2EX/B 站/即刻 等） | feedparser + HTML 剥离 |
| `twitter` | 单一用户 timeline | SocialData API · 需要 SOCIALDATA_API_KEY |
| `reddit` | 子版块 | 抓 reddit `.rss` 端点 · 无需 API key |
| `social` | 特定平台 dispatch | V2EX 已实现 · 其余推荐走 RSSHub |
| **`inbox`** | **反爬太强的长尾平台**（知乎/小红书/微博）| **web-access skill 落 markdown 到 `inbox/<slug>/*.md`，下次 collect 自动 ingest** |

### 🚨 智能告警 · LLM triage + 多重防误报

每 6h `alert service` 跑一次，看新消息 → **Haiku 4.5** 判断 *worth_alert*。命中三关才推送：

1. **具体新事件**（不是日常吐槽 / 不是现状回顾 / **不是旧事件余波**，事件本身距今 ≤ 7 天）
2. **跨源验证**（≥2 个独立频道讨论同一事件；单源仅"全行业冲击级"事件 + `【独家】` 前缀）
3. **业务相关性**（明确指向 topic 的监控对象）

防误报多层兜底：

- **24h headline dedup**（fuzzy match · SequenceMatcher ≥0.85 + 双向 substring，抓同义改写）
- **per-topic 频率 gate**（0/6/12/24/72/168h，避免低频 topic 浪费 LLM 调用）
- **`topic_sources.alert_enabled` 标志**（让"用户讨论区"类回声源只供 advisor/analyze，不进 alert triage）
- **首次回看窗口** `backfill_hours`（新 topic 第一次 alert 回看 N 小时，跑过自动清零）

命中后 Telegram push + 写 `02-告警归档/YYYY-W##-告警合集.md`（按周聚合）。

### 📊 深度报告 · Opus 4.7 出 8 字段议题分析

`analyze service` 人工触发或周日 22:00 weekly 跑。**Opus 4.7** 对一个 topic 的时间窗（默认 7d）消息聚类成议题，每议题输出 8 段，含 **4 个反向校验字段**抗 LLM 糊弄：

- 议题主线（headline / 影响范围 / 时间线）
- **❓ 反例信号**（什么样的证据会证伪当前判断）
- **🧪 什么会证伪**（12 个月内可观察的证伪事件）
- **⚠️ 误判风险**（采样偏置 / 信源立场 / 单地区等具体风险类型）
- **🔗 多源证实**（区分"事件本身多源 vs 归因单源"）

落 `01-报告/周报/YYYY-W##-{topic}-周报.md` 或 `01-报告/主题深度报告/YYYY-MM-DD-{headline-slug}.md`。

### 🎯 信源治理 · advisor 反馈环

用户在 Web UI 给 alert 打 `准 / 误报` 标签 → `advisor service` 看 30 天反馈窗口 + 信噪比，Haiku 给"加权 / 降权 / 关闭 / 加关键词"建议，**覆盖式更新** `03-主题/<topic>/信源.md` 的 `## advisor 建议` section（不破坏元信息和用户自定义段）。每 2-4 周跑一次，逐步剔除噪音源。

### 🤖 AI 主动驱动 · 3 个顾问 service (v2.2)

不是被动等用户操作，AI 主动建议优化：

| service | 触发 | 输入 → 输出 |
|---|---|---|
| **`coverage_audit`** | 周一 09:00 watchdog | 每 topic 算 4 指标 (msgs_7d / platform_count / failure_pct / signal_ratio) → severity (high/medium/ok) + LLM 诊断 + RSSHub 推荐 + web-access 关键词 |
| **`source_discovery`** | 新建 topic 时 Web UI 按钮 | topic name + industry + monitor_direction → LLM web search 推荐 5-10 真实信源 |
| **`keyword_advisor`** | topic 详情页按钮 | 现有 keywords + 最近 50 条样本 (alerted/unalerted 混合) → 5-15 关键词推荐 + reason + confidence |

### 🖥 Web UI · 完整后台

`python -m sentinel web` → `http://127.0.0.1:8080`（默认仅 localhost）。

| 页面 | 干啥 |
|---|---|
| `/` dashboard | KPI + 趋势 + 覆盖审计 banner + 失活源警告 |
| `/sources` | 信源管理（按 kind 分组 + 三层引导新建表单 + topic 关联）|
| `/topics` | 主题（alert / weekly / 频率 三 toggle + backfill + 覆盖审计 badge）|
| `/topics/{id}` | 详情（关键词 + AI 推荐关键词按钮 + 手动 analyze/advisor + 覆盖审计面板）|
| `/alerts` | 告警（filter + 展开 related messages + 一键标记 准/误报）|
| `/reports` | 周报 + 深度报告 + 告警归档浏览（filter + 上下篇导航）|
| `/status` | service_runs（LLM tok+cost · 立即跑 watch mode · 自动刷新）|
| `/cost` | LLM 成本按月聚合 |

### 🎙 三入口 · 同套 service API

- **Web UI**（推荐 · 含运维操作 + 写入校验）
- **CLI** (`python -m sentinel {collect,alert,analyze,advisor,deploy,status} ...`)
- **自定义 Claude Skill**（在 Claude 对话里说"跑下 X 周报"/"出 Y 信源建议" → skill 翻译成 CLI 命令 + 让你确认是否真跑）

### 🕰 launchd 长驻 · 6+1 plist 自动化

`python -m sentinel deploy install` 渲染 6 sentinel plist 到 `~/Library/LaunchAgents/`；社交平台用户可额外装本机 RSSHub 走第 7 个 plist。

- 4 触发型（collect / alert / weekly / backup）+ 2 长驻（watchdog 巡检 + web UI）
- **每天 09:00 watchdog** 巡检 collect/alert/backup/weekly 4 项 + 周一跑 coverage_audit + 月初 daily heartbeat
- **每天 23:30 backup** SQLite gzip 到 `<KB_ROOT>/_archive/backups/`（14 天滚动）+ rotate-logs（>10 MB 截断 .gz × 5）
- **多机感知** `.host` 文件 + `host_check`：副机跑同 service 自动 skipped，不污染主机数据

## 状态

**v2.2 (2026-05-17)** · 215 测试全过 · 4 service + 3 AI advisor + 6 collector kind · macOS launchd 接管 (6 plist + 可选 RSSHub plist)

- **4 主 service**: collect / alert / analyze ★ / advisor
- **3 AI 顾问 service** (v2.2): coverage_audit · source_discovery · keyword_advisor
- **6 collector kind**: telegram · rss · twitter · reddit · social · inbox
- **3 入口**: Web UI (推荐) · CLI · 自定义 Claude Skill

## 文档

| 文件 | 谁看 | 内容 |
|---|---|---|
| 本 README | 第一次接触 | 5 分钟跑通 + 路径约定 + 命令速查 + launchd 部署表 + Web UI 概览 |
| [docs/user-guide.html](docs/user-guide.html) | 日常运营 | 完整运营手册（50K · 下载到本地浏览器打开看更佳）：每个 service 详解 / Web UI 操作 / FAQ / 故障排查 |
| [docs/design.md](docs/design.md) | 想懂内部 | 系统设计 SSoT：架构 §1 / 数据模型 §2 / service 层 §3 / KB 结构 §5 / v2.1+v2.2 增量摘要 §11 / 4 条架构原则附录 A |
| [docs/architecture.svg](docs/architecture.svg) | 30 秒概览 | 视觉架构图（即上方嵌入版） |

## 5 分钟跑通

依赖：macOS · Python 3.12 · Anthropic 订阅（CLI 模式）或 ANTHROPIC_API_KEY (API 模式)。

```bash
# 1. 装依赖
brew install python@3.12
git clone <this-repo> sentinel-v2 && cd sentinel-v2
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. 配密钥
cp secrets/.env.example secrets/.env
chmod 600 secrets/.env
$EDITOR secrets/.env       # 填 TELEGRAM_API_ID/HASH 或 ANTHROPIC_API_KEY 等

# 3. 配主配置
cp config.yaml.example config.yaml
# cli_path 留空 → 启动自动 `which claude` 探测；想用 api 模式改 provider: api

# 4. 开 Web UI
python -m sentinel web      # http://127.0.0.1:8080
# 浏览器加 topic / source → 点 "立即跑 collect" 开始抓
```

## 路径约定

| 路径 | 用途 | 覆盖方式 |
|---|---|---|
| `~/sentinel-v2/` | 项目根（代码 + db + 配置） | env `SENTINEL_HOME` |
| `~/Documents/Sentinel/` | 产出根（报告 + 告警归档 + 主题信源.md） | env `SENTINEL_KB_ROOT` |

产出根包含：

```
~/Documents/Sentinel/
├── 01-报告/                                      analyze service 写
│   ├── 周报/                                    YYYY-W##-{topic}-周报.md
│   └── 主题深度报告/                              YYYY-MM-DD-{headline-slug}.md
├── 02-告警归档/                                  alert 写（按周聚合）
├── 03-主题/<topic>/信源.md                       collect/advisor 维护
├── inbox/<topic-slug>/*.md                       web-access 兜底落 markdown
├── _archive/backups/                             SQLite gzip 日备
└── 运行状态.md                                    service 自动维护
```

## 常用命令

```bash
# 手动跑 service
python -m sentinel collect run [--force]
python -m sentinel alert run [--force] [--dry-run-push]
python -m sentinel analyze report --topic "X" --period 7d
python -m sentinel analyze weekly               # 跑所有 weekly_enabled topic
python -m sentinel advisor scan --topic "X"

# 暂停 / 恢复
python -m sentinel <collect|alert> pause [--days N]
python -m sentinel <collect|alert> resume

# launchd 长驻部署
python -m sentinel deploy install   # 渲染 6 plist 到 ~/Library/LaunchAgents/
python -m sentinel deploy status
python -m sentinel deploy uninstall

# 状态查询
python -m sentinel status [--service collect|alert|analyze|advisor]

# 测试
pytest -v
```

## launchd 部署后

`deploy install` 渲染 6 plist：

| plist | 触发 | 行为 |
|---|---|---|
| `local.sentinel.collect` | 02 / 08 / 14 / 20 | 抓所有 enabled source |
| `local.sentinel.alert` | 02:30 / 08:30 / 14:30 / 20:30 | triage + push + KB 归档（按 per-topic 频率 gate） |
| `local.sentinel.weekly` | 周日 22:00 | 跑所有 weekly_enabled topic 的 7d 深度报告 |
| `local.sentinel.backup` | 每天 23:30 | SQLite gzip → `_archive/backups/` + rotate-logs |
| `local.sentinel.watchdog` | 每天 09:00 | 巡检 collect/alert/backup/weekly + 周一 coverage audit |
| `local.sentinel.web` | 长驻 | Web UI 127.0.0.1:8080，开机自启 + 崩溃自启 |

（可选）社交平台 (V2EX/B 站/即刻 等) 走 RSSHub：自己装 [RSSHub](https://docs.rsshub.app/)，配 launchd 长驻 :1200，source 用 `rss` kind 填 `http://127.0.0.1:1200/<route>`。

## Web UI

| URL | 功能 |
|---|---|
| `/` | dashboard · KPI / 趋势 / 覆盖审计 banner / 快失活源 |
| `/sources` | 信源（按 kind 分组 + 三层引导新建表单） |
| `/topics` | 主题（alert / weekly / 频率 toggle · backfill / 覆盖审计 badge） |
| `/topics/{id}` | 详情（关键词 + AI 推荐 + 覆盖审计面板 + 手动 analyze/advisor） |
| `/alerts` | 告警（filter + 展开 related messages + 一键标记） |
| `/reports` `/reports/view` | 周报 / 深度报告 / 告警归档 |
| `/status` | service_runs (LLM tok+cost / 立即跑 watch mode) |
| `/cost` | LLM 成本按月聚合 |
| `/health` | health-check JSON |

默认 `127.0.0.1:8080`，不监听外网。

## 多机感知

`deploy install` 把本机 hostname 写到 `<KB_ROOT>/00-系统设计/.host`。其他机器跑 service 会被 host check 退出，避免污染主机数据。切机：旧机 `deploy uninstall` → 新机 `deploy install`。

## License

MIT · 见 [LICENSE](LICENSE)
