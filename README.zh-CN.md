<div align="center">

<img src="docs/assets/queryforge-hero.svg" width="100%" alt="QueryForge — 从自然语言到可信 SQL 的受治理 AI 数据分析平台">

# QueryForge

### 从自然语言到可审计 SQL 的受治理 AI 数据分析平台

把业务问题转成安全、可追踪的 SQLite 查询，并提供语义层、策略治理、
有界修复和面向工程集成的多种交付接口。

[English](README.md) · [快速开始](#快速开始) · [系统架构](#系统架构) · [项目文档](#项目文档)

![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12-3776AB?logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-只读执行-003B57?logo=sqlite&logoColor=white)
![SQLGlot](https://img.shields.io/badge/SQL%20治理-SQLGlot-6B4FBB)
![Tests](https://img.shields.io/badge/tests-1080%20passed%2C%2025%20skipped-2EA44F)
![Offline acceptance](https://img.shields.io/badge/acceptance-13%2F13-7C3AED)

</div>

---

QueryForge 是一个本地优先、以数据域为第一入口的 AI 数据分析平台，它围绕一个核心原则设计：
**模型生成的 SQL 应该像应用代码一样被治理，而不是像自然语言一样被直接信任。**

用户先创建或选择数据域（如零售、金融、产品分析，或仓库内置的 Anime Streaming
示例域），再在该域内上传数据、评审语义契约和发起分析。项目把这套域级工作流与
NL2SQL、AST 级安全策略、只读执行、有界修复和完整运行产物串成闭环。

> Studio 面向本地单用户使用，默认以只读方式分析 SQLite 数据。

## 产品界面

<div align="center">
  <img src="docs/assets/queryforge-studio-domains.png" width="100%" alt="QueryForge 数据域中心：创建和选择相互隔离的业务上下文">
  <sub>数据域中心——先创建或选择治理边界，再接入该域的数据与语义。</sub>
</div>

<br>

<div align="center">
  <img src="docs/assets/queryforge-studio-overview.png" width="100%" alt="QueryForge Studio 总览：语义契约健康度、业务指标与互动趋势">
  <sub>数据域总览——Anime Streaming 只是当前选中的示例域，不再是平台身份。</sub>
</div>

<br>

<div align="center">
  <img src="docs/assets/queryforge-studio-semantic.png" width="49%" alt="QueryForge Semantic Studio 实体关系图和契约编辑器">
  <img src="docs/assets/queryforge-studio-analysis.png" width="49%" alt="QueryForge 受治理分析结果、生成 SQL 与 Trust Trace">
  <br>
  <sub>语义建模工作台 &nbsp;·&nbsp; 带可审计 SQL 和 Trust Trace 的受治理分析</sub>
</div>

## 为什么是 QueryForge？

大多数 NL2SQL Demo 在模型输出查询后就结束了，QueryForge 关注的是完整交付链路：

| 真实问题 | QueryForge 的处理方式 |
| --- | --- |
| 如何信任生成的 SQL | 使用 SQLGlot 解析 AST，执行前应用具名安全策略 |
| 如何保证业务口径一致 | 用 YAML 定义指标、维度、粒度和 Join Path |
| 如何防止上下文串域 | 数据源、语义契约、策略和运行历史全部绑定当前数据域 |
| 模型输出不完美怎么办 | 在明确预算内反思、修复和重试 |
| 复杂问题如何处理 | 启用有界 Schema 探索、Tool Loop 与可用的失败归因 |
| 如何追踪运行过程 | 保存状态、策略决策、质量证据和交付产物 |
| 如何接入其他应用 | 提供 CLI、REST/SSE、MCP、Gateway、图表和 HTML 报告 |
| 原始数据如何进入分析 | 从 CSV、Parquet 和分页 JSON API 构建受治理 SQLite 数据资产 |

## 快速开始

### 1. 安装

环境要求：**Python 3.11 或 3.12**；Studio 使用 **Node.js 22**。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
cp -n .env.example .env
```

### 2. 配置模型

在 `.env` 中选择一个 Provider。项目已包含 OpenAI-compatible、Claude、Gemini、
Qwen、DeepSeek 和 GLM 的配置。

```dotenv
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=your-api-key
```

Provider 默认值和环境变量映射见 [`models.yml`](models.yml)。

### 3. 运行内置示例

```bash
queryforge --prepare-sample-data

queryforge \
  --database sample_data/anime_streaming/anime_streaming.sqlite \
  --question "Which anime generated the most watch hours?"
```

体验动漫平台数据集上的多跳语义查询：

```bash
queryforge \
  --database sample_data/anime_streaming/anime_streaming.sqlite \
  --semantic-model sample_data/anime_streaming/semantic_model.yml \
  --sql-policy sample_data/anime_streaming/sql_policy.yml \
  --question "Compare watch completion and merchandise GMV by anime genre"
```

### 体验 QueryForge Studio

在工作台中创建数据域、上传 CSV/Parquet、审核语义并发布，然后查询、检查 SQL 和
Trust Trace，或回放绑定原数据版本的留存结果。

```bash
# 终端 1：QueryForge API
python -m pip install -e ".[api,assets]"
queryforge --serve-api --api-host 127.0.0.1 --api-port 8000

# 终端 2：QueryForge Studio
make web-install
make web-dev
```

打开 <http://localhost:3000>，从「Data Domains」进入：可选择内置 Anime Streaming
示例域，也可创建一个完全空白的新域，然后只在当前域内上传数据。Python API 离线时，
示例域分析会使用确定性的演示结果。详见 [Studio 使用指南](docs/studio.md)。

## 系统架构

<div align="center">
  <img src="docs/assets/queryforge-flow.svg" width="100%" alt="QueryForge 动态运行架构图">
</div>

对外生命周期保持精简：

```text
analysis → candidate → execution → completion → delivery
```

不同角色的 Agent 在这些阶段内部运行。确定性 Router 负责选择路径，模型不会控制安全边界。

## SQL 安全治理

每条模型生成的 SQL 都会经过可审计的处理链：

```text
SQL candidate
  → SQLite AST 解析
  → 单条只读语句
  → 表和列范围
  → 危险函数检查
  → 递归 CTE 与 Cross Join 检查
  → 表、Join 和 LIMIT 预算
  → 指标、筛选与有效输出核验
  → 受治理预览
  → 执行边界二次校验
```

策略可以非常精简：

```yaml
version: 1
name: anime_streaming
allowed_tables:
  - fact_watch_session
  - dim_anime
require_limit: true
max_limit: 500
max_tables: 2
max_joins: 1
allow_cross_join: false
```

策略拒绝会在 SQLite 执行前返回结构化 `SQL_SECURITY_ERROR`。

## 业务语义层

QueryForge 使用 YAML 语义模型补充原始 Schema 无法表达的业务上下文：

```yaml
metrics:
  - name: watch_hours
    description: Total valid viewing time in hours.
    entity: watch_session
    aggregation: sum
    expression: SUM(fact_watch_session.watch_seconds) / 3600.0
    default_filters:
      - fact_watch_session.is_valid = 1
    owner: audience-analytics
    sensitivity: internal
```

模型定义实体、维度、指标、粒度、关系和质量规则。查询默认要求经过校验的语义模型，
并自动发现数据库旁的 YAML。跨事实指标先独立聚合再连接，比例按分子分母重算，
各指标使用自己的事件时间、单位和覆盖边界。变化诊断提供分类贡献、独立总量对账和证据。

Studio 的数据接入流程：

```text
创建/选择数据域
  → 上传 CSV/Parquet
  → 审核实体、粒度、维度和指标
  → 确认 Owner 与敏感级别
  → 校验物理数据与语义合同
  → 发布数据 + 语义
```

新域从空白契约开始。Studio 提供单实体维度/指标编辑，完整关系、时间和单位在 YAML
中编写；物理发布成功后才能查询。默认技能预筛选和内容版本缓存减少无效上下文，修复与
候选预览受预算约束。历史回放保留原域和数据版本，报告过期后可隔离清理。

创建或增量更新语义层：

```bash
python scripts/build_semantic_model.py \
  --database warehouse.sqlite \
  --output warehouse.semantic.yml \
  --owner data-platform
```

构建报告会区分高置信度物理证据和必须由业务确认的定义，详见
[语义层构建指南](docs/semantic_authoring.md)和[语义契约](docs/semantic_contracts.md)。

仓库还包含每周一早晨运行的
[语义漂移检查](.github/workflows/semantic-weekly.yml)，会基于审核后的基线检查
Schema、指标、关系、Join Path 和数据质量契约。

### 内置示例数据域：Anime Streaming

Anime Streaming 是一个开箱即用的示例数据域，不是全平台 Schema。其数据是专为
QueryForge 构造的全合成数据：**370,762 行**、**15 张表**、
**30 条声明关系**、**7 条受治理 Join Path**、**11 个业务指标**，覆盖内容、
观看、订阅、广告、社区和动漫周边。

```mermaid
flowchart LR
  Studio[制作公司] --> Anime[动漫]
  Genre[类型] --- Bridge[动漫–类型桥表] --- Anime
  Anime --> Episode[剧集] --> Watch[观看会话]
  User[用户] --> Watch
  User --> Rating[评分] --> Anime
  User --> Subscription[订阅]
  Watch --> Ad[广告曝光]
  User --> Order[周边订单] --> Item[订单明细]
  Product[周边商品] --> Item
  Anime --> Product
  User --> Follow[用户关注] --> User

  classDef dimension fill:#111827,stroke:#7c3aed,color:#f9fafb;
  classDef fact fill:#172554,stroke:#22d3ee,color:#f9fafb;
  class Anime,Studio,Genre,Episode,User,Product,Bridge dimension;
  class Watch,Rating,Subscription,Ad,Order,Item,Follow fact;
```

可阅读[数据集说明](sample_data/anime_streaming/README.md)和
[语义模型](sample_data/anime_streaming/semantic_model.yml)，或执行
`python sample/generate_anime_streaming.py` 确定性重建数据库及可选 CSV 导出。

## 常用工作流

### 只生成并检查计划，不执行 SQL

```bash
queryforge \
  --plan-mode \
  --question "Compare monthly watch hours by subscription tier"
```

### 启用复杂执行模式

```bash
queryforge \
  --complexity-mode complex \
  --parallel-candidates 3 \
  --question "Explain completion-rate changes by genre, device, and membership tier"
```

### 流式查看进度或生成报告

```bash
queryforge --stream --question "List the top ten anime by watch hours"
queryforge --report --question "Build a report for monthly engagement by genre"
```

### 构建受治理数据资产

```bash
python -m pip install -e ".[assets]"

python scripts/scaffold_data_asset.py \
  --source events.csv \
  --output events.assets.yml \
  --owner engagement-analytics

# 审核语义草案，并将 semantic_model.reviewed 设置为 true。
python scripts/build_data_assets.py \
  --config sample_data/data_assets/assets.yml \
  --publish-database .queryforge/demo/analytics.sqlite
```

每个上传资产都必须携带实体语义。数据和语义模型原子发布，指标、关系、粒度或质量
契约任一失败都会回滚整批上传。

### 启动 REST API

```bash
python -m pip install -e ".[api]"
queryforge --serve-api --api-host 127.0.0.1 --api-port 8000
```

```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H 'content-type: application/json' \
  -d '{
    "question": "List the ten anime with the highest completion rate",
    "database": "sample_data/anime_streaming/anime_streaming.sqlite"
  }'
```

### 启动 MCP Server

```bash
python -m pip install -e ".[mcp]"
python -m queryforge.interfaces.mcp.server --transport stdio
```

## 接口与交付

| 方式 | 入口 | 适合场景 |
| --- | --- | --- |
| Studio | `make web-dev` | 数据域管理、可视化接入、语义编写与受治理分析 |
| CLI | `queryforge --question "..."` | 本地探索和工程工作流 |
| REST | `POST /ask`（会话路径）、`POST /analyze`（规划器路径） | 应用集成 |
| SSE | `POST /ask/stream` | 需要进度事件的客户端 |
| MCP | `queryforge.interfaces.mcp.server` | IDE 和 MCP 兼容助手 |
| Gateway | `POST /gateway/webhook` | 稳定的用户/渠道会话适配 |
| Artifact | JSON、Vega-Lite、SVG、HTML | 复核、分享和审计 |

## 项目结构

```text
queryforge/
├── cli.py             # 安装后的 CLI 实现
├── application/       # 与传输协议无关的服务门面和资源
├── core/              # 配置、共享 Schema、工作区路径解析和可观测性
├── data_assets/       # 数据接入、质量、血缘和发布
├── domain/            # SQL 策略、语义层、契约和 Skills
├── infrastructure/    # 数据库适配器、模型 Provider、存储和工具
├── evaluation/        # 基准阈值与评测侧契约规则
├── interfaces/        # API、MCP 和 Gateway 适配器
├── orchestration/     # Router、角色 Agent、生命周期和状态
├── workflow/          # NL2SQL 节点、候选选择、修复和报告
└── bundled_skills/    # 随包分发的 prompt-only Skill 定义

evaluation/gold/       # 多业务域 NL2SQL 评测集
evaluation/tasks/      # 确定性 Agent 基准任务（dev/regression/holdout）
sample_data/           # 可直接运行的 SQLite 数据集和语义模型
web/                   # QueryForge Studio 与本地 D1/R2 存储
scripts/               # 构建、基准、评测和验收工具
tests/                 # 单元、集成、边界和验收测试
docs/                  # 架构与功能文档
.github/               # CI、语义漂移审计和贡献模板
```

依赖从接口层和应用层向领域、基础设施与核心契约单向流动。

## 开发与检查

```bash
./init.sh             # 环境与离线测试
make check            # 仓库检查与完整验收
make semantic-check   # 语义模型漂移
make web-check        # Studio lint、类型、构建与测试
```

评测区分确定性治理测试和真实模型表现，方法、结果与限制见
[评测文档](docs/nl2sql_evaluation.md)。

## 项目文档

| 主题 | 文档 |
| --- | --- |
| 系统架构 | [Agent Team 架构](docs/agent_team_architecture.md) |
| 配置 | [配置参考](docs/configuration.md) |
| Studio | [可视化工作台与语义数据接入](docs/studio.md) |
| REST API | [API 参考](docs/api_reference.md) |
| MCP | [MCP Server](docs/mcp_server.md) |
| 语义层 | [语义契约](docs/semantic_contracts.md) |
| 语义层构建 | [构建、评审与强制语义模型](docs/semantic_authoring.md) |
| 数据资产 | [数据资产构建](docs/data_assets.md) |
| 评测 | [NL2SQL 评测](docs/nl2sql_evaluation.md) |
| 报告 | [报告产物](docs/report_artifact.md) |
| 主题域裁剪 | [Subject Tree](docs/subject_tree.md) |
| GitHub 发布 | [首次发布清单](docs/github_release.md) |
| 文档索引 | [全部指南](docs/README.md) |

## 贡献与安全

- [贡献指南](CONTRIBUTING.md)
- [安全策略](SECURITY.md)
- [行为准则](CODE_OF_CONDUCT.md)
- [变更记录](CHANGELOG.md)

仓库尚未声明开源许可证。

## 部署与安全

Studio 监听本地地址并限制同源请求；普通分析仅允许只读 SQL。REST/MCP 用于受控环境，
本地密钥放在被忽略的 `.env` 中。

- `QUERYFORGE_API_KEY`：启用 REST/Gateway 请求认证。
- `DATABASE_ALLOWLIST` / `REPORT_ROOT_ALLOWLIST`：限制数据和报告目录。
- `REPORT_RETENTION_DAYS`：设置报告访问有效期，默认30天。

SSE 客户端断开后在执行边界协作式取消；域内历史快照绑定原数据版本。部署配置见
[配置参考](docs/configuration.md)。

## Roadmap

- SQLite 之外的数据库适配器
- 一等的认证能力和租户策略边界
- 持久化工作流执行与取消
- 数仓 Catalog 集成
- 跨 Provider 的用量与成本核算

选择 Repository License 后即可正式开放外部贡献和设计讨论。
