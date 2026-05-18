---
tags:
  - sentinel/system
created: 2026-05-15
---

# Sentinel 系统设计 v2

> **VPN 行业舆情雷达 → 通用主题舆情雷达** · service 化重构 + 跨行业可扩展 + Web 后台。
>
> 起源：v1.2 完工后系统感觉"略复杂、易出错、糅杂"——做了一次复杂度审计（[[#债务地图]]），借鉴 aihot 项目的 4 条架构原则，整体重新设计。
>
> 状态：**草案 v2 · 2026-05-15**。架构图见 [[Sentinel 架构图.svg]]。本文档审过后将取代 [[设计.md]] 成为 SSoT，v1 设计.md 保留作历史。

## TL;DR

- **v2 = v1 五层骨架保留 + service 化拆分（4 service） + 跨行业 schema + Web 后台 + topic-led 信源**
- **4 个独立 service**：collect / alert / analyze ★ / advisor —— 互不阻塞、可单跑、按 topic 开关
- **数据源按"主题"组织**——用户面对的是 topic 不是 collector kind；collector 实现技术下沉到工具区
- **runtime.shell 抽出运维外壳**——业务核心不再写 pause / 阈值 / retry / caffeinate
- **fetchers 工具层**借鉴 web-access 沉淀（Python 实现，不依赖 LLM session）
- **新 KB 结构**：`03-主题/[topic]/信源.md` 含 advisor 建议 section（不另起目录）
- **fresh start `~/sentinel-v2/`**，旧 SQLite + 报告归档，旧 daemon 已暂停（`.pause` 2026-05-15）
- **名字保留 sentinel**

---

## §0 · 设计动机

### v1 痛点（来自复杂度审计 / 债务地图）

| # | 痛点 | 性质 |
|---|------|------|
| **A** | `daemon_main` 一个函数 19 步，业务逻辑和运维外壳混杂（caffeinate / pause / retry / retention / overview） | 偶然复杂度 — daemon.py 被 9-10 个 commit 反复改的根因 |
| **B** | social 链路跨世界拼接（scan-social skill → web-access → markdown → social collector → daemon ingest，5 跳跨 3 边界） | 偶然复杂度 — 但根因是抓取方式没封装好，**不是 social 不该在 sentinel** |
| **C** | collector 抽象被撑变形（social 是"读本地文件"、reddit 实际走 RSS，与 telegram "联网拉" 共用一个 BaseCollector） | 偶然复杂度 — 接口被撑 2 次 |
| **D** | analyze 4 个 prompt 模板（_SINGLE / _CLUSTER / _PER_ISSUE / _PER_SOURCE）+ 静默降级路径 | 偶然复杂度 — Phase 5 那个 timeout 坑就是这里来的 |
| **E** | 运维能力散落（4 plist + caffeinate + .pause + watchdog + auto-disable 等） | 主要是文档/导航问题 |

### 4 条架构原则（aihot 借鉴）

aihot skill 整个"系统"是 22KB 的 SKILL.md（0 daemon / DB / collector）。sentinel 不能照搬（它没有可外包的后端），但可以借鉴这 4 条**纪律**：

1. **SKILL.md 不堆逻辑** —— 只做"意图解析 → 调入口 → 格式化输出"。v1.2 scan-social 子命令在 SKILL.md 里写工作流逻辑，是反例。
2. **数据面 / 呈现面强分层** —— 拉数据、调 LLM、组装 markdown、写 KB 不能混在同一文件（v1 `analyze.py` 是反例）。
3. **边界关注点放在边界** —— 重试 / 阈值 / 缓存 / 限流是运维外壳，不渗到业务核心。
4. **pull-on-demand 与 daemon 分清** —— daemon 只承载需要后台不间断的事；其余靠用户触发。

### 跨产品 / 跨行业拓展需求

未来可能监控其他产品（my-product 之外）或其他行业（教育、电商）。v1 schema 没考虑这层。v2 加 `topic.industry` 字段轻量支持。

---

## §1 · 整体架构

详见 [[Sentinel 架构图.svg]]。

### 五层结构（自上而下）

| 层 | 内容 |
|---|------|
| **① 控制流** | 触发入口（launchd 自动 / Web UI / CLI 手动；Skill 是 CLI 的 AI 包装入口）→ service API → 触发 4 个 service |
| **② 数据流（主图）** | 4 列左→右：数据源（topic-led） → SQLite → 处理服务 → 产出 |
| **③ 工具/能力区** | Collectors 实现层 / Fetchers / runtime.shell / LLM models（不在数据/控制路径上） |
| **存储** | SQLite messages.db + Obsidian KB markdown |
| **入口/交互** | Web UI + CLI + Skill 三端调同一套 service API |

### 4 个独立 service

| service | 何时跑 | 输入 | 输出 | 调 LLM？ |
|---|---|---|---|---|
| **collect** | launchd 定时 + 手动 force | enabled sources | new messages → SQLite | 否 |
| **alert** | launchd 定时 + 手动 | new messages WHERE `topic.alert_enabled=1` | alerts → SQLite + push + KB 归档 | Haiku 4.5 |
| **analyze ★** | 人工触发（CLI/UI/Skill） | period + topic + industry | 深度报告 → KB | Opus 4.7 |
| **advisor** | 人工触发 | messages + alerts + user_label | 信源运营建议 → KB 信源.md 的 section | Haiku 4.5 |

**核心设计**：
- 4 个 service 独立可单跑（独立 entry / 独立 launchd plist / 独立 `.pause-<name>` 文件）
- 互不阻塞（共享只是 SQLite + KB 文件系统）
- 告警可单独开关（按 topic 粒度，`topic.alert_enabled` 字段），不影响采集和分析
- analyze ★ 是用户最核心功能：**人工触发深度报告**

---

## §2 · 数据模型

### topics 表加 2 个字段

```sql
ALTER TABLE topics ADD COLUMN industry TEXT NOT NULL DEFAULT 'VPN';
ALTER TABLE topics ADD COLUMN alert_enabled INTEGER NOT NULL DEFAULT 1;
```

- `industry` —— 行业分组标签（"VPN" / "教育" / "电商" / …），通过 `SELECT DISTINCT industry FROM topics` 即可枚举，**不引入独立 industries 表**（早期负担，需要时再实体化）。
- `alert_enabled` —— 主题级告警开关，alert service 只处理 `alert_enabled=1` 的 topic。

### daemon_runs → service_runs

```sql
ALTER TABLE daemon_runs RENAME TO service_runs;
ALTER TABLE service_runs ADD COLUMN service TEXT NOT NULL DEFAULT 'collect';
-- service IN ('collect', 'alert', 'analyze', 'advisor')
```

### 其他不变

- `sources`（kind = `telegram` / `twitter` / `rss` / `reddit` / `social`，collector kind 是内部实现细节）
- `topic_keywords`
- `topic_sources`（多对多）
- `messages`
- `alerts`（v1.1 已加 `user_label` / `user_label_at`）

### 用户视角的层级

```
industry（行业分组，topic 的 tag）
  └── topic（监控主题，用户面对的核心实体）
        └── sources（多个具体信源实例，可跨 topic 复用）
              └── kind（实现技术，系统内部）
```

---

## §3 · service 层架构

### §3.1 service 共享的运维外壳 `runtime.shell`

提取 `sentinel.runtime.shell` 模块（aihot 原则 #3 落地点）：

- caffeinate / pause 检查（`.pause-<service>` 文件）/ 阈值守门 / 回溯窗口 / service_run 记录 / 致命错误 push / retention（仅 collect 用）

每个 service 业务核心用 `with shell.run("<service>"): ...` 上下文管理器包装。**业务核心代码只关心业务**，运维外壳全在 shell 模块里。

### §3.2 collect service

**职责**：调度 collectors，把 messages 入库。

- 不调 LLM
- 输入：`SELECT * FROM sources WHERE enabled=1`
- 工作：遍历 source → invoke `collector = KIND_REGISTRY[source.kind](source, config)` → `async for msg in collector.collect(since): db.insert_message(...)`
- 单源失败隔离（单 source 异常不影响其他）
- source 连续 5 次失败 → auto-disable
- 每次跑完 `purge_old_messages(retention_days=90)`

### §3.3 alert service

**职责**：从 SQLite 订阅新 messages，做 triage，命中则推 + 归档。

- 输入：自上次 alert run 以来的新 messages，按 topic 分组
- triage prompt：v1.1 的三关 gate + 例外通道 + 二次收紧（防群聊接龙误报）保留
- 调 Haiku 4.5
- **按 `topic.alert_enabled` 开关**：`SELECT * FROM topics WHERE enabled=1 AND alert_enabled=1`
- 24h 同 headline 去重
- 输出：
  - alert 记录 → SQLite alerts 表
  - push → Telegram（命中即推）
  - KB → `02-告警归档/YYYY-W##-告警合集.md`（按周聚合）

### §3.4 analyze service ★

**职责**：人工触发，从 SQLite 读 messages，调 Opus，出深度报告写 KB。

- 触发参数：`--topic X` / `--period 7d` / `--industry VPN` / `--mode auto|single|timeseries`
- **数据/呈现强分层**（aihot 原则 #2 落地点）：
  - `analyze.data` —— 拉消息、过滤、聚类、议题展开 → 返回结构化 dict
  - `analyze.render` —— 把结构化数据组装成 markdown、命名 slug、写 KB
- **prompt 模板整合**：2 个（single / cluster + per-issue），统一 fallback（聚类失败仍走 per-issue 而不退回 per-source，消除 v1 那个静默绕过 4 反向校验字段的坑）
- 沿用 v1.2 反向校验 4 字段：❓反例 / 🧪证伪 / ⚠️误判 / 🔗多源
- 输出：
  - 周报 → `01-报告/周报/YYYY-W##-{topic}-周报.md`
  - 主题深度 → `01-报告/主题深度报告/YYYY-MM-DD-{headline-slug}.md`

### §3.5 advisor service

**职责**：人工触发，给信源运营建议。

详见 [[#§9 · advisor service 详解]]。

---

## §4 · 数据源组织（topic-led）

### 用户视角：主题主导信源

用户的 mental model：
1. 先有主题（业务关心的东西，如 my-product / 双减 / 协议封锁）
2. 给主题配置一组 sources（具体信源实例）
3. 同一渠道在不同主题下是独立 source 实例

**KB 上每个主题有自己的文件夹** `03-主题/[topic]/`，内含 `信源.md` 作为该主题的 SSoT（详见 [[#§5 · KB 结构]]）。

### 系统视角：collector kind 是内部实现

5 种 collector kind（实现技术，**用户不直接面对**）：

| kind | 实现 |
|---|---|
| `telegram` | telethon |
| `twitter` | SocialData API |
| `rss` | feedparser |
| `reddit` | praw / RSS 端点（v1.1 因 Anthropic policy 改走 RSS） |
| `social` | Fetchers 工具层（per-platform fetcher chain，含 v2ex / bilibili / jike / zhihu / xiaohongshu） |

新加 kind = 写一个 `BaseCollector` 子类即可，schema 不动，主图不动。

### 同渠道跨主题示例

```yaml
# 主题 A · my-product
- Telegram: @example_channel
- Twitter: @example_news
- 小红书话题: #VPN

# 主题 B · 双减
- Telegram: @edu-watch  # 不同 channel
- 知乎话题: #双减
- RSS: 教育部公告
```

两个主题都用 Telegram kind，但 sources 是不同实例（不同 channel / 关键词 / 监控方向）。这跟 v1 `topic_sources` 多对多 schema 完全兼容，**v2 不动 schema，改的是视觉呈现 + 用户认知**。

---

## §5 · KB 结构

```
<KB_ROOT>/
├── 总览.md                            # T0 静态入口（用户维护）
├── 运行状态.md                         # 各 service 自动维护
├── 00-系统设计/
│   ├── 设计.md                        # v1 历史（保留，本 v2 上线后改名加 _legacy）
│   ├── 设计 v2.md                     # 本文档（v2 SSoT）
│   └── Sentinel 架构图.svg            # v2 架构图（主干 + v2.2 增量层）
│   ├── Sentinel 系统全景图.svg          # v1 历史
│   └── Sentinel daemon 流程图.svg      # v1 历史
├── 01-报告/                            # analyze service 产出
│   ├── 周报/
│   │   └── YYYY-W##-{topic}-周报.md
│   └── 主题深度报告/
│       └── YYYY-MM-DD-{headline-slug}.md
├── 02-告警归档/                        # alert service 产出
│   └── YYYY-W##-告警合集.md            # 按周聚合
├── 03-主题/                            # **v2 新增** topic-led 组织维度
│   ├── my-product/
│   │   └── 信源.md                    # 该 topic 的 SSoT
│   ├── 双减/
│   │   └── 信源.md
│   └── ...
├── 使用说明/                           # HTML guide（v1 保留，v2 上线后更新）
└── _archive/                           # 历史归档
```

### `03-主题/[topic]/信源.md` 文档模板

```markdown
---
tags:
  - sentinel/topic-sources
topic: my-product
industry: VPN
---

# my-product · 信源记录

## 元信息
- industry: VPN
- alert_enabled: true
- 创建: 2026-05-XX
- monitor_direction: ...
- 关键词: vpn, 翻墙, ...

## 信源清单（sources）
- Telegram · @example_channel（@example_channel · 中文）
- Twitter · @example_news
- 小红书话题 · #VPN
- ...

## advisor 建议
> 最近更新: 2026-05-XX  ·  由 advisor service 自动写入

- @some-channel · 信噪比 0.01 · **建议关闭**
- @other-channel · 信噪比 0.45 · 建议保留
- 推荐添加关键词: "RUSEC", "TLS1.3"
- false positive 模式: ...
```

**关键约定**：
- `信源清单` 节由 collect/sentinel-config 流程维护（加/删 source 时同步）
- `advisor 建议` 节由 advisor service 重写（每次 advisor 跑覆盖此 section）
- 其他节由用户/AI 自由编辑（如主题笔记、人群画像等可逐步扩展进同目录）

---

## §6 · UI 范围（完整 Web 后台）

### 功能范围

**看（read）**：
- 告警列表 / 报告列表 / service 运行状态
- topic 清单（按 industry 分组）/ source 清单
- 单 source 信噪比、命中历史

**写（write）**：
- 加 / 改 source（含 industry + alert_enabled）
- 加 / 改 topic
- 给 topic 关联 sources
- 触发 analyze / advisor

**运维（ops）**：
- 暂停 / 恢复任一 service（`.pause-<service>` 文件操作）
- 看日志（实时尾巴）
- 手动 force 跑一次 service
- 标记 alert（user_label）

### 技术栈

**推荐 FastAPI + HTMX + Alpine.js**：单端口、单进程、无 npm 构建、跟"5 skill MVP"哲学一致。后期可平滑迁 React。

---

## §7 · CLI 入口

跟 service API 1:1 对齐：

```bash
# service 控制
python -m sentinel collect run [--force]
python -m sentinel collect pause [--days N] / resume
python -m sentinel alert run [--force]
python -m sentinel alert pause / resume

# analyze / advisor 人工触发
python -m sentinel analyze report --topic my-product --period 7d
python -m sentinel advisor scan --topic my-product

# 数据管理
python -m sentinel topic add --name X --industry Y [--alert-enabled]
python -m sentinel topic toggle-alert --name X
python -m sentinel source add --kind telegram --identifier @xxx
python -m sentinel source list [--industry X | --topic Y]

# 状态查询
python -m sentinel status [--service collect|alert|analyze|advisor]
```

**CLI = Web UI = Skill = 三个壳调同一套 service Python lib**。

---

## §8 · Skill 入口（简化）

旧 5 skill（router + config / analyze / status / daemon-ctl）→ v2 **1 个 skill**：

- `sentinel`（router + 意图分类 → 调对应 CLI / Python lib）
- **SKILL.md 严守原则 #1**：只翻译意图，不写工作流逻辑
- 子动作（加 source / pause / 跑报告 / ...）调下层 CLI

Skill 本质是 **AI 翻译过的 CLI**：用户在 Claude Code 说"跑一次周报"，Claude 解析为 `python -m sentinel analyze report --topic X --period 7d`。Skill 不是新的系统能力，是 UX 层。

---

## §9 · advisor service 详解

### 输入

```sql
-- 1. 各 source 近 N 天的命中率
SELECT s.id, s.kind, s.identifier,
       COUNT(DISTINCT m.id) AS total_msgs,
       COUNT(DISTINCT CASE WHEN am.id IS NOT NULL THEN m.id END) AS alerted_msgs
FROM sources s
LEFT JOIN messages m ON m.source_id = s.id AND m.posted_at >= datetime('now', '-30 days')
LEFT JOIN alert_messages am ON am.message_id = m.id  -- alerts 引用了哪些 messages
JOIN topic_sources ts ON ts.source_id = s.id
WHERE ts.topic_id = ?
GROUP BY s.id;

-- 2. 该 topic 近 30 天的 user_label 反馈
SELECT a.id, a.headline, a.user_label, COUNT(...) FROM alerts a
WHERE a.topic_id = ? AND a.user_label IS NOT NULL
GROUP BY a.user_label;
```

### 计算

- 信噪比 = `alerted_msgs / total_msgs`（每个 source）
- false positive 模式 = 用户标记的误报告警的共性（topic 关键词、来源、时段）

### LLM 总结

调 Haiku 4.5 让它把信噪比 + false positive 模式总结成自然语言建议：
- 建议关闭低信噪比 source（< 0.01）
- 建议加权高信噪比 source（> 0.3）
- 推荐添加 / 删除关键词
- 调整 topic.monitor_direction

### 输出

**写到 `03-主题/[topic]/信源.md` 的 `## advisor 建议` section**（覆盖式更新，每次跑重写此节）。**不另起 KB 目录**——advisor 建议是"信源运营"信息，属于信源记录的一部分。

### 触发频率

人工触发。建议每 2-4 周跑一次。**不进 launchd 自动**（avoiding 自动 LLM 调用堆积）。

---

## §10 · Fetchers 工具层（借鉴 web-access）

### 设计目标

把 web-access skill 的爬取知识沉淀转化为 Python collector 内部能力。**不在 daemon 中调 web-access skill**（避免把 LLM session 当数据管道）。

### 模块结构

```python
sentinel-v2/sentinel/fetchers/
├── base.py             # Fetcher 抽象接口
├── httpx_fetcher.py    # 直连抓 HTML / JSON
├── jina_fetcher.py     # r.jina.ai 抓 markdown（省 token）
├── cdp_fetcher.py      # playwright + 本地 Chrome CDP（复用登录态）
└── chain.py            # 降级链：httpx → jina → cdp
```

### per-platform 配置（在 social collector 内）

```python
SOCIAL_PLATFORMS = {
    'v2ex':        {'chain': ['httpx'],          'rate_limit': '1req/s'},
    'bilibili':    {'chain': ['httpx'],          'rate_limit': '1req/s'},
    'jike':        {'chain': ['httpx', 'jina'],  'rate_limit': '...'},
    'zhihu':       {'chain': ['jina', 'cdp'],    'rate_limit': '...'},
    'xiaohongshu': {'chain': ['cdp'],            'rate_limit': '极慢'},
}
```

### 资产复用清单

把 `~/.claude/skills/web-access/references/site-patterns/` 下已沉淀的 markdown（小红书 / 知乎已实测）翻译到 v2 SocialCollector 内的配置 + per-platform fetch 函数。

### web-access skill 命运

**保留作研究工具**——my-product-research step2 ① L1 / Vibe Coder 画像等"用户研究式探索"场景仍用 web-access skill。生产采集走 v2 fetchers。两者各司其职。

---

## §11 · 演进摘要 (v2.1 + v2.2)

本节列出 v2 实施后的 schema / 行为增量，**不含 implementation 时间线**。

### v2.1 hardening

- `topics.backfill_hours` — 新 topic 首次 alert 回看窗口（一次性，跑过清零）
- `topics.weekly_enabled` — 独立于 `alert_enabled` 的周报开关
- `service_runs.llm_*` — 5 列 LLM 成本（tokens_in/out, cache_read/creation, cost_usd），cli mode 取 claude CLI `total_cost_usd`
- alert per-topic 隔离：单 topic LLM 失败不挂整 run（partial 语义）
- alert dedup 改 fuzzy match（lower + 去标点 + SequenceMatcher>=0.85 + 双向 substring，抓同义改写）

### v2.2 增量

**3 个 AI 顾问 service**（跟主 4 service 同级，复用 ServiceShell + llm_usage 框架）：

| service | 触发 | 输入 | 输出 |
|---|---|---|---|
| `coverage_audit` | watchdog 周一 / 手动 | 每 topic 算 4 指标 (msgs_7d / platform_count / failure_pct / signal_ratio) | severity (high/medium/ok) + LLM 诊断 + RSSHub 推荐 + web-access 关键词 → 写 `topic_coverage_audit` 表 |
| `source_discovery` | 新建 topic UI 按钮 | topic name + industry + monitor_direction | LLM web search 推荐 5-10 真实信源 (kind+identifier+display+reason+confidence) |
| `keyword_advisor` | topic 详情页按钮 | 现有 keywords + 最近 50 条样本 (alerted/unalerted 混合) | 5-15 关键词推荐 + reason + confidence |

**Schema 增量**：

- `topics.alert_interval_hours` — per-topic alert 频率（0=用全局 6h；12/24/72/168 = 每 12h/每天/每 3d/每周）
- `topics.last_alert_checked_at` — 上次 alert 处理时间（per-topic since 起点 + gate 依据）
- `topics.coverage_threshold_overrides` — 覆盖审计 per-topic 阈值覆盖（JSON, NULL=全局）
- 新表 `topic_coverage_audit` (severity / metrics_json / diagnosis_md / rsshub_suggestions_json / webaccess_suggestions_json / status pending/executed/dismissed)

**新 collector**：

- `inbox` kind — 监视 `<KB_ROOT>/inbox/<topic-slug>/*.md`，ingest 后改名 `.md.ingested` 防重复（web-access 兜底通道）

**新增的 v2.2 后修复**（详见 git log）：

- `delete_topic` / `delete_source` FK 处理：删 topic 前 `UPDATE alerts SET topic_id=NULL`（历史保留为孤儿）；删 source 前级联删 messages（NOT NULL 不能 SET NULL）
- `topic_sources.alert_enabled` — 让 "用户讨论区" 类回声型源仅供 advisor/analyze，不参与 alert triage
- Triage prompt 加 "事件 freshness ≤ 7d" + "旧事件余波" 反例
- RSSHub 本机部署接入（社交平台知乎/小红书/B站/即刻/微博 改走 `rss` kind 接 `http://127.0.0.1:1200/<route>`，绕过 social kind 反爬维护负担）



## 附录 A · 4 条架构原则与 v2 落地点

| 原则 | v2 落地点 |
|---|---|
| #1 SKILL.md 不堆逻辑 | `sentinel` skill 只翻译意图 + 调 CLI；旧 scan-social 那种工作流不再写在 SKILL.md |
| #2 数据面 / 呈现面强分层 | `analyze.data` vs `analyze.render` 拆分；UI 层独立于 service 层 |
| #3 边界关注点放在边界 | `sentinel.runtime.shell` 模块独立承担 pause / 阈值 / retry / retention |
| #4 pull-on-demand vs daemon 分清 | analyze / advisor 是 pull；collect / alert 是 daemon；social 平台抓取在 daemon 内（封装在 fetcher 工具层），不依赖 LLM session |

---

