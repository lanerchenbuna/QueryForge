# Data Agent 优化任务与验证记录

审查基线：`d1e1e4f`。本计划沿用 DatabaseTool、PlanValidator、SQLPolicyEngine、
EvidenceStore 和运行日志，不引入第二条 SQL 执行路径。

| 阶段 | 任务 | 完成条件 | 状态 |
| --- | --- | --- | --- |
| 1：回答正确性 | 多指标不遗漏；明确过滤进入查询；同比/环比使用指定期间；未知终态失败关闭；请求与结果交付核验 | 原审查案例有回归测试；不支持的组合明确澄清；CLI/REST、证据、预算和恢复回归通过；`./init.sh` 与 `make check` 通过 | 已完成（2026-10-09） |
| 2：分析语义 | 跨事实指标按粒度聚合；比例分子分母；时间角色、单位、可加性；期间覆盖与完整性 | 多粒度、缺失期间、零分母和跨事实独立数据 oracle 验证；语义漂移门禁通过 | 已完成（2026-10-10） |
| 3：业务诊断与评测 | 完成变化→分解→质量核验→证据交付；任务级必需列和排序验收；独立 schema 留出；重复配对实验 | 真实业务诊断离线验收；评测不容忍必需指标遗漏；模型实验报告样本量、区间与失败分母 | 开发、真实 DeepSeek 配对评测与离线审计完成（2026-10-11）；结果和最终验收见下文 |
| 4：效率与本地产品化 | 请求与指标绑定；技能预筛选、有界候选与内容版本缓存；拆分请求/血缘/产物职责；真实 Studio 行为与产物生命周期 | 同输入离线合同及调用量对比；上传→发布→查询→拒绝→取消→回放；本地来源、域/版本隔离和过期控制 | 核心开发与自动验收完成（2026-10-11）；仅本地单用户，浏览器上传完整点击验收待复验，真实模型效率未重测 |

## 第一阶段范围

- 支持相同实体、默认过滤和时间口径的多指标汇总与分类结果；不同口径的组合
  明确要求澄清，不选取首个指标冒充完成。
- 支持基表列与字面量构成的明确过滤（比较、AND/OR、IN、BETWEEN、NULL）；
  不接受子查询、函数、注释、多语句或无法解析的自然语言过滤。
- 明确同比/环比按日历期间匹配；缺少指定基准时报告部分结果，不替换成其他期间。
- 在现有查询和回答节点核对请求指标、过滤、维度、时间窗口与比较要求；显式失败的状态不会
  被包装成成功。
- 跨事实多指标、任意自然语言过滤、复杂归因与完整期间策略留给后续阶段。

## 分阶段任务清单

| 编号 | 优化任务与交付物 | 验收方式 |
| --- | --- | --- |
| 1.1 | 请求解析保留多个指标；编译同口径聚合；交付所有指标列及证据 | 汇总、分类、缺失列与不兼容口径的独立数据用例 |
| 1.2 | 字面量过滤绑定语义列；核验 SQL 中的必需谓词；隔离引号里的时间/分组词 | 正常过滤、空切片、未知列、子查询、注释、过滤被遗漏或 OR 放宽的用例 |
| 1.3 | 日期/日期键按日、月、季、年聚合；日历基准精确配对 | 同月同比、月内多日聚合、跨年环比、缺失期间与结果上限用例 |
| 1.4 | 未知终态失败关闭；交付约束进入日志指纹；替代维度不能完成原请求 | 终态测试、调用方自定义计划、原样恢复、CLI/REST、全量离线回归 |
| 2.1 | 跨事实多指标先按目标粒度聚合，再连接；定义合法维度与可加性 | 多事实、多对多和比例指标 oracle；拒绝 fan-out 与错误分母 |
| 2.2 | 显式时间角色、单位、时区和期间完整性；比较查询自动纳入所需基准窗口 | 跨时间角色、缺失/不完整期间、闰日、周和财务日历用例 |
| 2.3 | 将上述口径写入语义合同、作者校验和漂移检查 | 合同兼容性测试、样例模型迁移与 `make semantic-check` |
| 3.1 | 变化诊断串联期间比较、贡献分解、异常与质量解释；逐项输出证据和限制 | 真实业务形状的离线案例，分解合计和残差可复核 |
| 3.2 | 评测明确必需输出列、排序、空结果和任务完成要求；修复只比共同列的问题 | 故意删除一个指标、乱序或改口径时，评测必须失败 |
| 3.3 | 多 schema 留出；固定候选数、技能配置与输入；重复配对实验 | 追踪数据集/代码/配置版本，报告样本量、区间、环境错误及付费支出 |
| 4.0 | 根据真实失败样本收紧请求→指标绑定、有效表达式与输出列合同 | 缺失CSAT计数、SLA编号列表、独立订单/明细计数和未使用聚合CTE的基表oracle、CLI/REST与错误注入 |
| 4.1 | 技能关键词预筛选、内容版本缓存与候选预览预算修复；保留单候选默认与原有修复流程 | 固定任务合同、调用量与估算 token；元数据/正文改变、预算及取消回归；真实模型质量/效率另行授权 |
| 4.2 | 拆分请求合同、输出血缘、CSV 检查、快照与报告存储职责；修复 Studio 的真实状态与交互 | HTTP 上传/发布/查询/拒绝/取消/回放；浏览器查询/回放；原生上传点击验收待复验 |
| 4.3 | 按用户确认仅支持本地单用户：loopback/同源边界、域与数据版本绑定、产物过期、可见失败 | 外来 Origin、路径覆盖、跨域回放拒绝；快照过期与报告可恢复隔离；不新增多用户登录系统 |

阶段按 1 → 2 → 3 → 4 推进。每阶段关闭对应回归与验收后再进入下一阶段；
付费模型实验和部署架构分别按 AGENTS.md 的授权与评审要求执行。

## 第一阶段行为变化

数据 fixture：3 条记录，金额分别为 10、20、30；类别为 a、b、a。
时间比较 fixture：2024-02-01=10，2025-01-01=20，2025-02-01=30。

| 原问题 | 审查时的行为 | 第一阶段结果 |
| --- | --- | --- |
| item count 和 total amount，按 category | 仅执行第一个指标，仍成功 | a=(count 2, amount 40)，b=(count 1, amount 20)，全部列带证据 |
| total amount where item_id = 1 | 丢弃过滤，返回 60 | 编译并核验 `items.item_id = 1`，返回 10 |
| for category a / with supplier_code 'x' | 未解析的筛选可能变成分组或全量结果 | 执行前要求明确字面量比较，`needs_clarification` |
| monthly trend year over year | 以 2025-01 的 20 为基准，得到 50% | 以 2024-02 的 10 为基准，对 2025-02 的 30 得到 200% |
| 未知或缺失结果状态 | 默认成功 | `failed`；`unsupported` / `unverified` 归为 `blocked` |
| 按品牌无数据后改按类别 | 替代查询成功后，整个原请求成功 | 替代证据保留，原请求因维度未满足而 `blocked` |

兼容性说明：月度比较标签改为 `YYYY-MM`，避免跨年月份合并。现有单指标字段保留，
多指标添加 `metrics`、`values`、`columns`。查询达到结果上限时保守拒绝“最新期间”
结论；自动扩展比较窗口与期间完整性策略在阶段 2 实现。本阶段只扩展 SQLite 的
确定性分析路径，不据此声称真实模型准确率提升。

## 验证证据

2026-10-09，基于 `d1e1e4f` 的第一阶段工作区变更。环境：Python 3.12.11、
SQLite 3.50.4、SQLGlot 28.10.1、Pydantic 2.13.4。

| 检查 | 命令 | 实际结果 |
| --- | --- | --- |
| 开发前基线 | `./init.sh` | 928 项，903 通过，25 skip，0 失败 |
| 新增专项回归 | `LOG_LEVEL=CRITICAL .venv/bin/python -B -m unittest tests.test_analysis_intent_regressions -q` | 30/30 通过；包含实际 SQLite、CLI、REST、自定义计划和恢复执行 |
| 开发后全量验证 | `./init.sh` | 959 项，934 通过，25 skip，0 失败，退出码 0 |
| 仓库与完整离线验收 | `make check` | 仓库检查通过；13/13 验收通过；内含的全量测试同样为 959 项、25 skip、0 失败；退出码 0 |
| 语义漂移检查 | `make semantic-check` | 82/82 通过，0 blocking failures，`no_change` |
| 差异格式 | `git diff --check` | 退出码 0 |

新增测试总计 31 项（30 项请求正确性回归、1 项不可验证终态回归）。
测试以 fixture 的独立期望值和样例库的独立 SQL 结果核对数值，未调用真实模型，
未进行付费 tier-3 评测。上述测试计数已同步到版本控制内的 README；评测器严格列
校验、多 schema 泛化及真实模型效果仍按阶段 3 验收。

## 第二阶段交付（2026-10-10）

第二阶段在第一阶段工作区上增量开发，沿用原执行、预算、证据和恢复机制。

| 任务 | 实现与闭环 |
| --- | --- |
| 2.1 跨事实与比例 | 每个指标在自己的事实表、过滤和时间口径上独立聚合，再通过去重键域连接；连接键实际唯一性和其他事实粒度经 DatabaseTool 核验。比例显式声明 SUM/COUNT 分子分母，按每个目标粒度重算，输出审计列；AST 同时核验分母、缩放和数值除法，AVG 等价需证明相同 NULL 口径。零分母、缺失事实组和不可相加的总计拒绝交付成功。 |
| 2.2 时间与覆盖 | 声明单位、可加性、事件角色、UTC/固定时差和财务起始月；周一为周起点；基准窗口自动纳入查询；每个请求指标单独比较。完整性按已审核的入库覆盖边界执行，拒绝不完整/缺失期间、缺失当前期间、窗口截断和非法日期。无覆盖契约时明确标记未验证。 |
| 2.3 契约与漂移 | 向后兼容的可选分析契约；比例组件 AST 与物理列校验；发布与运行时校验日期；四个样例指标迁移。漂移检查先发现四个预期指标变更且 86 项合同全部通过，审查变更后更新基线，再验证 no_change。恢复指纹纳入模型内容和实际解析日期，口径或相对窗口改变后重算。 |

独立 fixture：items 金额为 10、20、30，charges 金额为 5、7、3。
两个事实都按品牌 a/b 连接，但不连接事实明细。

| 场景 | 实测结果 |
| --- | --- |
| 跨事实按品牌查询 | a=(item amount 40, charges 12)，b=(20, 3)，无明细 fan-out |
| 不同默认过滤 | count=3；amount 在 amount>10 的独立口径下=50，保留各自口径元数据 |
| 比例的不同粒度 | 金额 10、100、30；a=(分子40/分母2)=20，b=100；总体=140/3≈46.6667，避免简单平均成60 |
| 2025-02 明确同比 | 自动补入 2024-02；amount 30 对 10，变化 200%；多指标分别提供比较 |
| 周粒度跨年 | 2024-12-23=10，2024-12-30=50（含 2025-01-01），按周一起点归属 |
| 零分母、重复连接键、多对多、缺失期间、2月30日 | 显式部分/不支持/阻断，均不能成功交付 |
| 财务年度4月起始 | 季/年标签为实际期间起始日期；月末不溢出到错误期间 |
| 同版本口径变更后的恢复 | 比例 scale 从1改为100、单位变为 cents/item 后，20 重算为2000；不复用旧结果 |
| 相同自然语言相对窗口后的恢复 | 解析窗口从2025-01变成2025-02后，20重算为30；不复用旧结果 |

样例库独立 SQL 核验（完整覆盖 2023-01-01 至 2025-12-31）：

| 指标 | 2024-02 基准 | 2025-02 当前 | 变化 |
| --- | ---: | ---: | ---: |
| watch_hours（小时） | 1115.8683333333333 | 1117.3080555555555 | +1.4397222222221444 |
| average_rating（1–10分） | 7.38747680890538 | 7.261959957850369 | −0.12551685105501154 |

上述数值来自 `tests.test_analysis_semantics.test_bundled_cross_fact_comparison_matches_independent_sql`
中的两条独立只读 SQL，按各自事件日期键的年月分组；不复用编译器表达式或执行输出计算 oracle。
当前实现保持 SQLite 确定性分析范围。命名 DST 时区、半可加快照的跨时间聚合、
命名财年的自然语言窗口仍明确不支持；类别×期间诊断分解在阶段3交付。
覆盖边界是入库方的审核承诺，不能由事实表最小/最大日期推断出来。

### 第二阶段验证记录

环境：Python 3.12.11、SQLite 3.50.4、SQLGlot 28.10.1、Pydantic 2.13.4。
新增 `tests/test_analysis_semantics.py` 共49项测试，覆盖真实 SQLite、CLI、REST、
发布/漂移、恢复、独立样例 oracle 及错误注入。

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 开发前基线 | `./init.sh` | 959项，25 skip，0失败，退出码0 |
| 第二阶段及相关语义专项 | `LOG_LEVEL=CRITICAL .venv/bin/python -m unittest tests.test_analysis_semantics tests.test_semantic_sql_validator tests.test_anime_semantic_governance -q` | 101/101通过，其中49项为第二阶段新增；退出码0 |
| 样例迁移审查 | `make semantic-check`（基线更新前） | 检出4个预期指标变更；86/86合同通过，0 blocking failures |
| 审查后基线更新 | `LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/check_semantic_drift.py --database sample_data/anime_streaming/anime_streaming.sqlite --model sample_data/anime_streaming/semantic_model.yml --baseline sample_data/anime_streaming/semantic_baseline.json --report /tmp/queryforge_phase2_baseline_update.json --update-baseline` | baseline_updated，86/86合同通过 |
| 语义复核 | `make semantic-check` | no_change；86/86通过，0 blocking failures，退出码0 |
| 离线任务门禁复核 | `LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/benchmark_agent.py --tier 1 --gate --report /tmp/queryforge_phase2_tier1.json` | 23/23任务通过；既有必需步骤和证据全部满足，阈值未改 |
| 最终全量 | `./init.sh` | 1008项，983通过，25 skip，0失败；退出码0 |
| 仓库与完整离线验收 | `make check` | 仓库检查通过；13/13验收通过；内含全量测试1008项、25 skip、0失败；退出码0 |
| 差异格式 | `git diff --check` | 退出码0 |

新增测试验证治理、业务口径与控制流，不度量真实模型能力。本阶段未调用付费 API，
未运行 tier-3，也不将离线通过率解释为模型准确率提升。

闭环期间发现：将所有 DISTINCT 计数一律禁止分组总计，会移除零售订单数任务的
必需 drill_down 证据。修复为可证明的例外：仅当 COUNT DISTINCT 的列恰好是
单列事实主键/粒度，运行时完整唯一性检查通过且连接不复制记录时，才按互斥分组
累计。跨组用户去重等业务 DISTINCT 仍不可相加；受限扫描或未知唯一性仍被拒绝。
该修复保留既有验收定义，并新增可相加/未验证不可相加的两项回归。

比例错误注入还发现：严格核验数学表达式时，不能把合法的最终结果 ROUND
误判为分子分母变化。修复允许最终展示精度，仍拒绝对组件先行舍入；AVG
等价路径检查物理非空或相同 COUNT 列，拒绝可空列的 COUNT(*) 口径和分组均值再平均。
既有样例的合法舍入与 AVG 查询恢复通过，新旧语义测试共同验证，没有放宽验收断言。

## 第三阶段交付（2026-10-11）

本阶段接续前两阶段工作区，不改 SQL 执行边界、确定性门禁或只读默认值。
离线实现与模型实验分开验收；下列开发及后续授权的 DeepSeek 实验均已完成。

| 任务 | 实现与闭环 |
| --- | --- |
| 3.1 业务诊断 | 有日历粒度和明确基准的分类请求自动生成诊断计划：质量核验、独立总量、期间×分类明细、精确比较、贡献分解、异常扫描、证据交付。每个指标单独对账；当前和基准两期的分类绝对总量都必须匹配独立查询，不仅验证差值。输出 `answer.diagnosis`，保留单位、口径、完整性、质量检查和证据 id；贡献是算术分解，不是因果证明。 |
| 3.2 严格交付验收 | 共享 `result_contract`：必需列、额外列、外层排序、空结果策略和结果路径。默认比较完整参考投影；辅助列必须显式声明。列名对齐后逐位置比较行内数值，保留重复行；NL2SQL 必须运行成功才算结果正确。使用漏指标、值换位、排序丢失、重复/空行、失败终态和非有限数值错误注入。 |
| 3.3 独立留出与配对 | 物理 schema 指纹与 `split_scope` 防止仅改数据集名跨分片；保留问题/模板/语料污染检查。候选实验固定 skills=auto，技能实验固定候选数；同任务同输入重复运行，交替 A/B 顺序，每次状态隔离。按任务聚类 bootstrap，单列环境和已知 provider 协议失败，记录有效分母、原始证据、模型/技能资产、代码、数据、合同、金标和运行环境指纹；运行中版本变化不能宣告实验完成。 |

### 可复核的业务结果

独立 fixture 两期分类：a=30→10，b=20→50。

| 场景 | 实测结果 |
| --- | --- |
| 对账与相互抵消的驱动 | 独立总量50→60，变化+10；a贡献−20、b贡献+30，贡献份额−200%和300%，残差0；份额允许超出0–100%，避免掩盖抵消关系 |
| 总变化为零 | a贡献−20、b贡献+20仍保留；总变化0，贡献份额为null，不能编造百分比 |
| 多指标诊断 | count和amount分别比较、分解和对账，最终证据支持每个指标 |
| 未知/丢失的类别 | NULL是独立桶；缺失类别期间只在完整覆盖下解释为0；连接丢失即使两期丢失相等、变化残差恰好为0，也因绝对总量不符而拒绝 |
| 有限输出、不可加、证据缺失 | 达到查询上限、比例/非可加维度、无完整性依据的空桶、删掉诊断节点的自定义计划均不能成功交付 |
| 异常历史不足 | 显式两期查询只有2点，异常状态为insufficient_data并带限制；不把“没有足够历史”解释为“没有异常” |
| 对外接口与恢复 | CLI和REST交付一致数值与证据；模拟中断后重放仍区分独立总量与明细证据，不复活已结束运行 |

`evaluation/diagnostics/` 新增三条任务，未修改原32条任务或阈值。
金标数值由 `scripts/freeze_diagnostic_tasks.py` 的独立只读基表 SQL 冻结；
总量 SQL 不复用分类 SQL 或运行输出。浮点残差的绝对容差显式设为1e-9，
既有任务的零值仍默认精确匹配；不是把残差不符改为通过。

| 独立 schema / 任务 | 基准→当前 | 变化 | 期间完整性 |
| --- | ---: | ---: | --- |
| anime / watch_hours 按设备同比（2024-02→2025-02） | 1115.8683333333333→1117.3080555555555小时 | +1.4397222222221444 | complete，已有覆盖契约 |
| retail / net_revenue 按渠道环比（2024-02→2024-03） | 12071.96→10791.91 | −1280.05（浮点舍入） | unverified，未给旧样例补造覆盖契约 |
| support / ticket_volume 按优先级环比（2024-02→2024-03） | 31→32 | +1 | unverified，未给旧样例补造覆盖契约 |

这三条任务均通过完整投影、期间排序、独立总量、贡献残差和证据链验收。
生成器金标与当前样例 SQL 的一致性及三条真实执行已加入单元套件。

### 重复配对验证的含义

两类离线实验各选择原金标中23条workflow任务、三个独立schema、每任务5次重复、
两组设置：各230次执行、115对观察。两组任务通过率均为1.0，变化为0，
任务聚类bootstrap 95%区间为[0,0]；环境/协议失败排除数为0，版本指纹稳定。
这是 ScriptedModel 直接返回 reference_sql 的治理与控制流验收，**不能度量模型能力**，
零宽区间不能证明机制没有效果。没有实测模型token或价格表，token/成本变化均为null。
离线延迟不用于评判真实模型效率。

复现命令、任务契约和报告解释见[评测方法](nl2sql_evaluation.md#phase-3-task-contracts-and-paired-experiments-2026-10-11)。
原始JSON报告生成于 `/tmp/queryforge_phase3_paired_candidates.json` 和
`/tmp/queryforge_phase3_paired_skills.json`；数字保存在本跟踪文档中。
依赖版本：Python3.12.11、SQLite3.50.4、SQLGlot28.10.1、Pydantic2.13.4；
每个报告的manifest另含source/assets/datasets/contracts/gold的SHA-256和git状态。

付费实验已准备dry-run预览：沿用仓库配置的deepseek / deepseek-v4-flash，
候选1→2（skills=auto）及技能off→auto（候选数=1），各23任务×5重复×2组=230次执行。
预览不调用API；当时模型选择、预算、账户余额和凭据配置尚待确认。
病例执行次数不等于API调用上限，修复和多轮上下文会增加调用。
[AGENTS.md](../AGENTS.md) 明确要求："Tier-3 / any real API spend → ask before running."
未授权前不运行tier-3，不能宣称真实模型准确率、延迟或成本改善。

后续用户已授权使用 DeepSeek，并在本机配置凭据。API 核验当前模型为
`deepseek-flash` / V4.1-Flash，账户可用。原控制流题集的首次真实预检发现题意
与标准投影不一致，因此暂停并保留预检记录；新增独立的 `evaluation/live_tasks/`
明确请求列、排序和空结果，保留原数据、参考 SQL、32条任务和阈值。
真实实验、使用量对账和成本口径见[实时评测记录](nl2sql_evaluation.md#authorized-deepseek-live-pairs-explicit-tasks-v1-2026-10-11)。

### 第三阶段验证记录

新增40项测试：诊断16项、输出/物理隔离/工具追踪14项、配对实验10项。
闭环期间全量发现Demo E仍使用无独立总量证据的手写宽表计划；迁移为正式诊断模板，
保留原有“数据下降→持平→增长，结论随数据改变”的断言。演示6项复核通过。
同时把基准工具调用计数改为逐个tool span，避免一个查询步骤内的日期/粒度核验被漏计，
并避免并发步骤共享预算计数相互重叠。三条诊断实测10/8/8次工具调用，均符合原阈值。

| 检查 | 命令 | 实际结果 |
| --- | --- | --- |
| 开发前基线 | `./init.sh` | 1008项、25 skip、0失败，退出码0 |
| 诊断/输出/配对/原基准专项 | `LOG_LEVEL=CRITICAL .venv/bin/python -B -m unittest tests.test_analysis_diagnostics tests.test_evaluation_contracts tests.test_paired_benchmark tests.test_agent_benchmark tests.test_agent_task_gold -q` | 61/61通过，退出码0 |
| 演示修复复核 | `LOG_LEVEL=CRITICAL .venv/bin/python -B -m unittest tests.test_demo_scripts -q` | 6/6通过，退出码0 |
| 原默认门禁 | `LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/benchmark_agent.py --tier 1 --gate --report /tmp/queryforge_phase3_tier1.json` | 23/23通过，阈值未改，退出码0 |
| 独立留出与原全分片门禁 | `LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/benchmark_agent.py --tier 1 --split dev --split regression --split holdout --gate --report /tmp/queryforge_phase3_all_splits.json` | 32/32通过（含9 holdout），阈值未改，退出码0 |
| 新诊断门禁 | `LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/benchmark_agent.py --tier 1 --tasks-dir evaluation/diagnostics --split dev --split regression --split holdout --gate --report /tmp/queryforge_phase3_diagnostics.json` | 3/3通过；完整投影和独立总量均满足；退出码0 |
| 重复配对（候选） | `LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/run_paired_benchmark.py --tier 1 --feature candidates --repeat 5 --report /tmp/queryforge_phase3_paired_candidates.json` | 115/115对，230/230执行通过；无环境/协议失败；版本稳定；退出码0 |
| 重复配对（技能） | `LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/run_paired_benchmark.py --tier 1 --feature skills --parallel-candidates 1 --repeat 5 --report /tmp/queryforge_phase3_paired_skills.json` | 115/115对，230/230执行通过；无环境/协议失败；版本稳定；退出码0 |
| 语义漂移 | `make semantic-check` | 86/86通过，0 blocking failures，no_change，退出码0 |
| 最终全量 | `./init.sh` | 1048项，1023通过、25 skip、0失败，退出码0 |
| 完整仓库验收 | `make check` | 仓库检查与13/13离线验收通过；内含1048项、25 skip、0失败；退出码0 |
| 差异格式 | `git diff --check` | 退出码0 |

单独 `--split holdout --gate` 会因原阈值要求regression分片而退出1；这是预期的完整性门禁，
不能删掉该要求以得到绿色结果。本阶段使用三分片一起运行门禁，并独立检查holdout的9/9结果。

### DeepSeek 真实配对评测与补充修复

已按用户授权使用 `deepseek-flash`（API发现名称 DeepSeek-V4.1-Flash），
完成候选1→2及技能off→auto两组实验：每组23题、3个独立schema、5次重复、230次执行。
两组共460次执行，执行期间的代码、资产、数据、合同与金标指纹一致。
原始分数和后验审计分开保存，没有改阈值或重跑挑选结果。

| 实验 | 原始合同通过率（每组115次） | 补充来源核验（每组115次） | 剔除歧义题后的来源核验（每组110次） | 最后一列差值及任务聚类95%区间 |
| --- | --- | --- | --- | --- |
| 候选1→2，技能auto | 91/115→84/115：79.13%→73.04% | 89/115→82/115：77.39%→71.30% | 89/110→82/110：80.91%→74.55% | −6.36个百分点，区间[−13.64,0] |
| 技能off→auto，候选数1 | 91/115→88/115：79.13%→76.52% | 89/115→85/115：77.39%→73.91% | 89/110→85/110：80.91%→77.27% | −3.64个百分点，区间[−8.18,0] |

最后一列口径是单独报告的后验敏感性分析，包含22题×5次重复；每条原始记录仍保留。
被剔除的排名题使用“outlet”，而数据中 `store_format=Outlet` 是具体类型，原金标却要求
所有24家门店。按类型筛选四家门店有题意依据，这是金标题的问题，不能作为模型错误。
未来v3题集明确要求所有门店、不限类型；没有用改写后的题意重判旧回答，也没有再调用模型。

| 效率指标（同22题子集，按任务等权） | 候选1→2 | 技能off→auto |
| --- | --- | --- |
| 平均耗时 | 21.916→24.073秒，+9.84%；差值区间包含0 | 12.704→23.690秒，+86.47%；差值区间[7.813,15.158]秒 |
| 实测token | 18498.70→31011.02，+67.64%，110对 | 15075.89→18621.41，+23.52%，109对 |
| 已知价格费用均值 | 0.021003696→0.025221900元/次，+20.08%，110对 | 0.012204155→0.020640890元/次，+69.13%，109对 |

技能组有一次反思请求超时，原失败保留在质量与耗时分母中，没有重跑。
先前四次成功响应的35164个token可以核对，第五次调用用量未知；整次调用的总费用为null，
不能当作免费。这一对不进入实测token与完整费用比较，所以对应分母为109对。
数值来自缓存命中/未命中/输出分别计价，属于当前缓存条件下的估价，不能当作冷缓存成本或账单。
候选已报告响应估价5.27165492元，技能已报告响应小计3.79053536元；合计9.06219028元，
不含未知调用。账户余额及预检余额观察保留在私密本地记录，不公开展示。

两种机制均未证明质量提升。区间按任务聚类抽样，重复留在任务内部，不把110对当作110条独立题目。
这里评价的是终态、输出列、数值、证据和工具记录的完整交付合同；列名错误、已恢复的工具失败
也可能不通过，不能称为纯SQL生成准确率或通用能力。完整命令、指纹、分母与限制见
[冻结评测记录](nl2sql_evaluation.md#authorized-deepseek-live-pairs-explicit-tasks-v1-2026-10-11)。

本次补充开发均在两组付费实验结束后合入：

| 优化成果 | 实测闭环 |
| --- | --- |
| 独立SQL来源合同 | 仅计入最终查询有效作用域的物理表，别名和未使用CTE不能冒充来源；不支持的形状失败关闭。共拒绝9次只查日历表的误通过；独立SQLite反事实数据证明日期极值相同只是样例巧合。来源覆盖是必要条件，不证明连接、过滤或完整业务语义。 |
| 可复算审计与费用 | 重算460条原始判定，0不一致；核对1560条已报告响应，0用量不一致；估算与实测、未知调用费用分开。原始报告、执行版本与后验评分版本均保留。 |
| 断点续跑 | 技能实验暂停后的175条记录原样保留，仅执行剩余55条；恢复前离线复现175/175判定并核对全部控制。独立CLI恢复验证保留2条、只执行剩余2条，4/4通过，0真实响应。 |
| SQL实际重放 | 经DatabaseTool核对全部原始通过SQL：候选175/175、技能179/179，实际行与参考值均匹配；原策略哈希与数据库字节不变。重放仍不能证明源表意图，因此同时保留独立来源检查。 |
| 多轮实验日期控制 | 修复AgentService评测分支未收到固定日期的问题，同时显式传递重试/向量控制；两轮真实Scripted回归均使用2024-12-31。原实验的该题统计全部订单，没有相对时间窗口，值不受运行日影响；原日期控制缺陷如实保留。 |
| 未来金标修正 | v1原样保留；v2只增加来源评分条件；v3只澄清门店排名题。三版各23条独立只读SQL oracle全部核验通过；更改题意不能回溯评分旧输出。 |

新增30项回归（来源13、合同/审计/金标16、多轮日期1），第三阶段累计新增70项。
补充审计与SQL重放均没有调用模型。专项184项通过；最终 `./init.sh` 实测1078项、
1053通过、25跳过、0失败，退出码0（95.934秒）。

第四阶段优先新增4.0：收紧请求→指标绑定及有效表达式核验。缺失满意度数量不应附带平均满意度，
SLA编号列表不应强制聚合计数，独立订单/明细计数应按事实粒度核验；未使用的AVG CTE不能满足
一个真正需要执行的平均值义务。相应闭环应覆盖基表oracle、SQL作用域、CLI/REST和错误注入。
再进行4.1的技能预筛选与候选预算，按同输入质量、耗时、token与费用验收。上述是第三阶段
结束时的记录，当时尚未实施第四阶段；本地第四阶段的后续交付与限制见下文。

### 补充开发最终验收

| 检查 | 复现命令 | 实际结果 |
| --- | --- | --- |
| 来源/审计/原评测专项 | `LOG_LEVEL=CRITICAL .venv/bin/python -B -m unittest tests.test_sql_source_coverage tests.test_source_contract tests.test_agent_evaluator tests.test_agent_task_gold tests.test_evaluation_contracts tests.test_paired_benchmark tests.test_agent_benchmark tests.test_analysis_diagnostics -q` | 184项通过，0失败，退出码0（5.135秒） |
| 三版金标 | `scripts/freeze_live_tasks.py --check`；同命令加 `--source-contract` / `--correct-questions`（使用 `.venv/bin/python -B`） | v1/v2/v3各23条独立oracle核验通过，均退出0 |
| 全量 | `./init.sh` | 1078项，1053通过、25跳过、0失败，退出码0（95.934秒） |
| 仓库验收 | `make check` | 仓库检查通过、13/13离线验收通过；内含1078项、25跳过、0失败，退出码0（内含测试98.705秒） |
| 原三分片门禁 | `LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/benchmark_agent.py --tier 1 --split dev --split regression --split holdout --gate --report /tmp/queryforge_phase3_deepseek_final_all_splits.json` | 32/32通过，原阈值未改，退出码0 |
| 诊断门禁 | 上述命令加 `--tasks-dir evaluation/diagnostics`，报告 `/tmp/queryforge_phase3_deepseek_final_diagnostics.json` | 3/3通过，退出码0 |
| 差异格式 | `git diff --check` | 退出码0 |

全量日志为 `/tmp/queryforge_phase3_deepseek_final_init.log` 和
`/tmp/queryforge_phase3_deepseek_final_make_check.log`。真实运行原始JSON、来源评分JSON、
逐调用用量、断点和DatabaseTool重放记录均在被忽略的 `evaluation/reports/` 中；
本记录及现有跟踪文件 `docs/nl2sql_evaluation.md` 保存数值、命令和依赖版本。

## 第四阶段：本地单用户交付（2026-10-11）

原计划共四个阶段。用户确认“仅支持本地单用户”后，4.3 按本地边界、域/版本绑定和
产物生命周期实施；没有增加多用户登录或云部署授权。保留 DatabaseTool 唯一 SQL 执行
边界、确定性政策/语义/证据门禁及只读默认。代码开发和自动验收完成，完整浏览器上传
点击验收仍待复验；不能把该项标记通过。第四阶段没有调用付费模型。

### 实现与独立核验

| 任务 | 交付与证据 |
| --- | --- |
| 4.0 请求与有效输出 | 正式返回列名与业务指标分开；核验最终列名/顺序；编号列表拒绝聚合、常量和变换。跟踪 CTE/派生列的有效血缘，未使用 AVG 或 GROUP BY 不满足口径；独立事实计数按各自作用域验证，连接 fan-out 仍拒绝。显式支持的 NULL 请求不可省略、取反或被 OR 放宽。单基表可信非空单列主键证明后才允许 COUNT(*) 等价。实际 SQLite、CLI/REST 和错误注入回归验证执行前拒绝。 |
| 4.1 调用与缓存 | 十个内置技能增加关键词预筛选；没有可选技能时直接使用默认 SQL 技能，保留 `auto` 状态。显式主题目录/手动选择兼容。缓存仅含元数据和 prompt，按真实字节/正文版本失效；同 mtime 改动、删除文件及跨节点覆盖有回归。保留默认单候选及原有修复流程；非法/重复候选不消耗预览预算，取消阻断后续生成/预览/执行。 |
| 4.2 Studio 真实状态 | 提取 CSV、来源状态、快照和本地边界模块，新增可编辑指标表达式/聚合/维度。CSV 检查真实有界样本，Parquet 交由 Python 物理发布；不虚构数据质量、行数或覆盖分数。实际 publication/data_version 才显示 Ready/Published；未知和失败状态可见。修复默认指标未限定表名、上传格式与发布器不一致、成功合同仍显示 Draft、旧域结果滞留及历史 run_id 不一致。 |
| 4.3 本地生命周期 | Studio 上游和开发监听限定 loopback；外来 Origin 拒绝。域未发布/域与路径混用均阻断。历史快照绑定 run/domain/data_version，2 MiB 上限、30 天访问，跨域404、冲突409、缺失/过期410；回放不查询、不调模型。报告默认30天后不可访问，清理先预览，执行后移入 `.expired/` 可恢复，不删除运行证据。 |

新增 Python 测试27项（请求16、本地产品化8、候选预算/取消3），总数1078→1105；新增
Studio 行为测试7项，总数24→31。维护热点按请求合同、输出血缘、报告存储与前端边界
拆分，没有另造 SQL 执行器。缓存不存用户结果；快照是本地客户端提交的留存结果，不是
针对不可信多用户的签名证明。契约编辑器只覆盖单实体维度/指标，完整关系、时间、单位仍在
YAML 编写。关键词是启发式筛选，未命中业务同义词时仍需补充目录。

### 固定题集的离线效率观察

比较 `evaluation/live_tasks_v3` 全部23题（含一条多轮任务），数据、合同、金标、阈值和
任务次序指纹一致，自动技能模式、固定日期2024-12-31、无向量/历史检索。
前版本未显式传候选数，离线 runner 默认1；后版本显式1。逐题检查两次都没有候选选择
payload，因此候选数没有混入比较。前版本已经实施主要4.0修复；后版本同时包含后续
正确性修复和技能资产变化，这是版本观察，不能当作单因素随机消融。

| 指标 | 预筛选前 | 最终版本 | 解释 |
| --- | ---: | ---: | --- |
| 任务合同通过 | 23/23 | 23/23 | SQL 由 ScriptedModel 提供，只验证工程控制流与确定性门禁 |
| 模型调用次数 | 72 | 51 | 少21次，29.17%；包含多轮任务早先的一轮 |
| 末轮技能路由模型 span | 23 | 3 | 另有多轮任务早先一轮的调用计入上一行 |
| 本地估算 token | 299851 | 276994 | 少22857，7.62%；实测调用/token 均为0 |

未对并发运行离线夹具的毫秒耗时作性能结论；没有 DeepSeek 准确率、实际延迟或账单成本
改善的证据。阶段3付费实验没有证明候选2或全目录技能路由提升质量，因此默认单候选保留。
真实模型复验需要新的付费授权及有余额账户，不因离线全绿而替代。

前版本命令：

```bash
LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/benchmark_agent.py --tier 1 --tasks-dir evaluation/live_tasks_v3 --split dev --split regression --split holdout --report /tmp/queryforge_phase4_live_v3.json --gate
```

最终命令：

```bash
LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/benchmark_agent.py --tier 1 --tasks-dir evaluation/live_tasks_v3 --split dev --split regression --split holdout --parallel-candidates 1 --skill-mode auto --report /tmp/queryforge_phase4_live_v3_final.json --gate
```

运行环境 Python3.12.11、SQLite3.50.4、SQLGlot28.10.1、Pydantic2.13.4；基于
`d1e1e4f8ef95dfdf16bf7206ddcb797fa881eb4a` 的未提交工作区。前/后 source 指纹分别
`83e6adde9def7439bb9823901846bbe5503c2bf3e6ac5168a531a1b589423a20` /
`37691987be54e082f14a271c07801492fede4f45b6961f1205fd290b26a26b4f`；
金标指纹 `50573fa619a41660f7f548cb38598b42dc204f92708622b5b589edf1f1c01842`。
完整数据/合同/资产指纹、Node版本、前端字节指纹和证据摘要保存于
[第四阶段冻结记录](../evaluation/phase4_local_acceptance_2026_10_11.json)。原始报告保留在 `/tmp/`，
上述数值与依赖已写入待提交的仓库文件，不依赖被忽略的原始 JSON 才能查阅。

### 闭环验收

| 检查 | 命令 | 实测结果 |
| --- | --- | --- |
| 开发前基线 | `./init.sh` | 1078项、25跳过、0失败，退出0 |
| 最终全量 | `./init.sh` | 1105项：1080通过、25跳过、0失败，退出0；110.680秒 |
| 仓库与离线验收 | `make check` | 仓库检查、13/13验收通过；内含1105项、25跳过、0失败，退出0 |
| 语义漂移 | `make semantic-check` | 86/86通过，0失败，no_change，退出0 |
| Studio 静态/构建/行为 | `make web-check` | eslint、tsc、生产构建与31/31测试通过，退出0 |
| 原全部分片门禁 | `LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/benchmark_agent.py --tier 1 --split dev --split regression --split holdout --report /tmp/queryforge_phase4_tier1_all.json --gate` | 32/32通过，原阈值未改，退出0 |
| 诊断任务门禁 | 上一命令加 `--tasks-dir evaluation/diagnostics`，报告 `/tmp/queryforge_phase4_diagnostics.json` | 3/3通过，退出0 |
| v3 全分片门禁 | 本节上方最终命令 | 23/23通过，退出0 |
| 实际本地 HTTP/SQLite 闭环 | `.venv/bin/python -B scripts/check_local_studio.py --report /tmp/queryforge_phase4_studio_acceptance.json` | 24/24通过，0真实模型调用，退出0；夹具慢请求用于取消验收 |
| 差异格式 | `git diff --check` | 退出0 |

HTTP checker 必须先看到显式离线模型健康标记，否则退出。真实服务启动方式见
[Studio 验收说明](studio.md#validate-the-frontend)，临时 D1/R2、发布数据库、运行日志
均与样例库/用户存储隔离。实际3行 CSV/Parquet fixture 金额10/20/30：上传→审核→Python
发布返回可执行 data_version，查询 count=3、sum=60；非法 CSV/Parquet、未审核/未发布域、
路径覆盖、DELETE、外来 Origin、跨域/冲突快照均拒绝，拒绝后记录仍为3。SSE 唯一终态和
连续序列通过；生成阶段断开连接后状态落盘 cancelled，未生成执行结果产物。取消是协作式，
在途模型请求可能先完成，不能承诺立即中止该请求。过期报告404、dry-run 保留原件、实际
清理可恢复两份文件，原运行证据保留。

另通过 CUA 在真实 Chrome 验证已发布域的查询：SQL 为
`SELECT COUNT(DISTINCT record_id) AS n FROM uploaded_record`，可见结果3；随后从历史
回放相同结果和原 data_version，模型调用计数24→24。该域由 HTTP 夹具先发布，不能将
浏览器查询证据称为浏览器上传证据。完整上传点击流程未通过：合成 CSV 在原生选择器中
被选中，但“打开”一直禁用；未伪造 file-input 成功。该项保留待手工复验，真实 HTTP
发布24项和浏览器查询/回放分别记录。阶段4的真实 DeepSeek 质量、延迟、实测用量也未重测。

最终日志：`/tmp/queryforge_phase4_final_init.log`、`/tmp/queryforge_phase4_make_check_final.log`、
`/tmp/queryforge_phase4_web_check_final.log`、`/tmp/queryforge_phase4_studio_acceptance.log`。
快照原始 HTTP 和浏览器记录分别为 `/tmp/queryforge_phase4_studio_acceptance.json`、
`/tmp/queryforge_phase4_browser_acceptance.json`；核心摘要已冻结到上述仓库 JSON。
