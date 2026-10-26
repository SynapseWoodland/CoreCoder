# Dogfood 计划：先让可观测性可读，再决定优化什么

- 文档版本：v1.8（2026-10-26）
- 读者：本仓库维护者（未参与前序讨论，需要照做即可执行）
- 定位：把「先建评测集，还是先做作者留下的优化项」的讨论结论，落成有依赖顺序、有验收标准的执行计划
- 关联文档：`optimize/agent-loop-must-optimize.md`（循环缺陷分析）。本文不重复那部分内容，也不复核已实施的 M1–M4
- 路径约定：除特别说明外，路径相对仓库根目录；`~/.corecoder` 指用户主目录下的观测数据目录

> 本文只回答三件事：现在该做什么、按什么顺序、做完怎么验收。
>
> **数字声明**：文中统计值均为实测后写下的那一次读数（主体 v1.2，3.1 与 3.2 的缺陷表 v1.5 重测，
> 0B 的 B1/B4/B5/B7/B8 与第 9、11 项在 v1.6 逐条对照代码复核过，B1/B4/B5/B8 与 C2 在 v1.8 落地并按
> 修复后的实测改写）。
> 观测数据是活的，会随运行次数增长，也会被保留策略裁掉（见 3.3），复核请直接跑附录 A 的命令，
> 不要拿本文的数字当基线。本文不含代码行号，
> 引用一律用符号（函数名、文件名、常量名）。
>
> **v1.1 增量**：补充 Langfuse 侧实测（3.4）、观测层修复清单（阶段 0B，对应第 1–7 项）、
> 以及三条暂缓项的实施触发条件（第 5 节，对应第 8–10 项）。v1.0 正文未改写。
>
> **v1.2 增量**：Tracing / Audit 重构（0.9.0）后逐条复验。3.1 与 3.4 全部重测；B3、B6 已部分落地并改写；
> 新增 3.5（重构带回的四个问题）与阶段 0C（其中三个的修法）；第 8 项的过期读数删除；
> `observability` 模块改名 `audit` 后的符号引用全部更新。
>
> **v1.3 增量**：阶段 0A 与 B2 已落地（只动了 `tests/conftest.py` 与 `.env`，`corecoder/` 一行未改）。
> 0-1 / 0-2 / 0-4 / B2 标为完成并附实测验收；0-3 与剩下的 B 组、C 组仍是待做；
> 阶段 1 开头补了两条真实运行产出的观察。
>
> **v1.4 增量**：C1（N1）已修，本轮开始动 `corecoder/agent.py`；3.1 与缺陷 2 的读数重测；
> 测试总数从 352 涨到 353（新增的那一条就是钉住 C1 的用例）。
>
> **v1.5 增量**：缺陷 2 与 0-3 / B3 已修——阶段 0A 四条至此全部落地，`run_summary` 的字段全部可用。
> 3.1 读数重测，并补了两条带成本与错误类型的真实观察。
>
> **v1.6 增量**：`corecoder/` 零改动，只做一次「文档 vs 代码」的逐条复核。改了两条：B4 的表述按实测
> 重写（正文其实按轮记在 generation 上，缺的是 root/trace 层与三个兜底返回值），B1 补一条「传入链路
> 已通、剩余成本两行」。B5、B7、B8、N2/C2、N4 与第 9、11 项经核对**仍然成立**，原文保留。
> 3.3 另补显式归档路径的两笔代价。
>
> **v1.7 增量**：新增阶段 0D —— 把 audit 的保留上限从写死的 10 做成配置项 `CORECODER_AUDIT_KEEP`。
> 阶段 1 因此**不再需要任何搬文件或改路径的归档动作**；3.1 与 3.3 按新口径重写。
>
> **v1.8 增量**：0B 与 0C 至此收口。B1、B4、B5、B8 与 C2 五条一起落地（`corecoder/tracing.py`、
> `corecoder/agent.py`、`corecoder/tools/agent.py` 与 `tests/`），0B 只剩 B7，0C 三条全部完成。
> **两条先前的判断被实测推翻**，改写处都留了原因：C2 落地走了与原文相反的方向（见 0C），
> B5 的症状与 3.4 原记的恰好相反（见 0B）。
> 另带回一条新限制：子 agent 现在与父同会话，一个用户回合可能留下两行 `run_summary`（见 3.3 第 4 条）。

## 1. 结论

**先 dogfood，但先花一天让观测数据可读。**

顺序是「清噪音 → 只跑不改 → 固化用例 → 按证据优化」，四段有硬依赖：

1. 观测数据现在**读不出真信号**（audit 侧实测比例 511:3；Langfuse 侧同样混池，见 3.4），
   在这一层上做任何决策都是拿 `pytest` 的通过率当产品质量。清噪音不是一句口号，是阶段 0 的十五条具体动作。
2. dogfood 是唯一能回答「我写下的规格对不对」的手段，单元测试结构上回答不了这个问题。
3. 作者清单里 4 条待做项，**只有 2 条可以不依赖真实数据先做**，另外 2 条必须有 dogfood 的用例才能验证收益。

一句话禁止：**不要为了建评测集而去建评测集**。评测集的正确来源是 dogfood 中反复出现的真实卡点，
不是凭空想象的 50 条任务（理由见 2.2）。

## 2. 为什么不是「先建评测集」也不是「先做优化」

### 2.1 作者清单已过期，照单做会重复劳动

`article/07-build-your-own.md` 的「你可以往哪走得更远」列 5 条，`README_CN.md` 的对应段落列 4 条，
两份清单不一致，且都有已完成项：

| 文档说法 | 代码事实 | 核对方式 |
|---|---|---|
| article 将「接 MCP」列为待做 | 已实现：`mcp.py` 覆盖 `initialize` / `tools/list` / `tools/call`，REPL 有 `/mcp` | 读 `mcp.py`；README_CN 有整节 |
| 两份文档均称「八个工具」 | `ALL_TOOLS` 实为 9 件（含 `fetch_url`） | 读 `corecoder/tools/__init__.py` |
| article 称全量测试 227 条 | 实为 354 条（v1.5 复跑） | `pytest tests/ -q --collect-only` |

**结论**：清单没有单一事实源。动手前先以代码为准校准，别把「文档说没做」当成「真的没做」。

### 2.2 四条待做项按「是否需要评测判定收益」分类

| 待做项 | 判定标准是什么 | 需要评测吗 | 结论 |
|---|---|---|---|
| bash 真沙箱 | 二值：危险命令被拦、正常命令照跑 | 不需要，单测即可 | 可提前做 |
| fallback 模型链 + 美元预算闸 | 二值：主模型故障时切备用、累计超预算时停 | 不需要（机制层面），注入故障单测即可 | 可提前做，但依赖成本读数 |
| 并发区分读/写 | 概率性：并发度改变会不会引入写冲突、变得 flaky | 需要 | 等 dogfood 用例 |
| 子 agent 异步 / worktree | 概率性：隔离是否干净、汇总质量是否下降 | 需要，且最贵 | 最后做 |
| RAG / 检索式代码定位（仅 README 列） | 强依赖模型与仓库：找不找得到代码 | 最需要 | 最后做 |

所以「先评测还是先优化」这个二选一本身不成立：**5 条里 2 条不需要评测，3 条离开评测就无法验证。**

### 2.3 评测集不该凭空建

评测集的边际价值来自「能区分两个版本」。手上没有可比较的两个版本时，先建集等于猜指标。
业界通行的形态是四层，成本递增，个人项目通常只做到 L2：

| 层 | 形态 | 单次成本 | 本仓库现状 |
|---|---|---|---|
| L0 契约测试 | 单测钉死循环不变式（截断拒执、异常回填、结果治理） | 秒级，免费 | 已有，354 条 |
| L1 离线回放 | 用 `ScriptedLLM` 编排响应，重放固定轨迹 | 秒级，免费 | 素材已有（`tests/`、`examples/m2`–`m4`），缺聚合跑批 |
| L2 任务级私有集 | 真实仓库快照 + 一句指令 + 机器可判验收 | 分钟级，要 API 费 | **没有**，且应来自 dogfood |
| L3 公开 benchmark | SWE-bench Verified 等 | 小时级，要容器 + 费用 | 不做 |

L2 的验收器最便宜的形态是**用项目自己的测试当裁判**：给 agent 一份注入了 bug 的仓库副本，
让它把指定测试跑绿。零标注成本，且天然可复算。

## 3. 观测数据现状（主体为 v1.2 重测；3.1 与缺陷表 v1.7 已重测）

### 3.1 信噪比：405 : 6

按 `model` 字段统计 `run_summary` 事件（`~/.corecoder/audit.*.log`，11 个文件，共 411 条；v1.7 复测）：

| model | 运行次数 | 性质 |
|---|---|---|
| `scripted-demo` | 368 | demo 与测试 |
| `fake` | 20 | 测试替身 |
| `gpt-4o` | 17 | 测试 |
| **`mimo-v2.6-flash`** | **6** | **真实使用** |

真实运行仍是 6 次，其中只有 3 次带 `build=dogfood`——打标是 v1.3 落地的，更早三次没有。

**本节数字从 v1.7 起只涨不跌**：保留上限已做成 `CORECODER_AUDIT_KEEP`（见 0D），默认路径不再
被剪到 10 个。在这之前它一路被剪：577 行（v1.4）→ 419（v1.5）→ 411（v1.7，其中 `--demo` 那次
仍按内置上限淘掉了一个旧文件）。

历史本来就不靠 audit 留：同一批运行在 Langfuse 里 **205 → 2814 条一条没少**（自托管库不裁），
而 audit 被 `keep=10` 从 809652 字节裁到 575763。所以 audit 的定位是「最近现场 + 一份免费的
纯文本副本」；要回看完整现场得查 trace，而这条路**v1.8 起通了**（B1/C2/B4 已落地，拿审计行的
`trace_id` 能直接命中一条带 `level`/`tags`/`output` 的 trace）。

trace 侧现在只能看 Langfuse：`CORECODER_TRACE=langfuse` 之后 `traces.*.jsonl` 不再新增（最新一批仍是
10-25 的读数），而 Langfuse 里的污染比 audit 更重——最近的抽样里连 `tool.writer`、`tool.boom`、
`tool.exiter` 这类**只存在于测试里**的工具名都成了独立 trace
（见 3.5 的 N3）。trace 总量从 v1.1 的 205 条涨到 2814 条，增速几乎由「跑了几次测试」决定；
0-1 之后这条曲线也该停了，但**存量的测试产物不会自己消失**。

**含义**：现在打开仪表盘，看到的主要是测试套件在给自己打分。

复核请直接跑附录 A.1，**不要拿本节数字当基线**。

### 3.2 已确认的三个数据层缺陷

| # | 缺陷 | 证据 | 影响 |
|---|---|---|---|
| 1 | ~~测试往真实目录写观测数据~~（v1.3 已修） | `Agent.__init__` 不传 tracer 时回退 `create_tracer()`，继承 `.env` | 跑一次测试就多一百条，仪表盘读不出真实使用 |
| 2 | ~~`run_summary` 不带错误类型~~（v1.5 已修） | `Agent._log_run_summary` 现在也写 `error_type` | 从「知道失败了」到「知道为什么失败」 |
| 3 | ~~成本读数为 `None`~~（v1.5 已修） | `~/.corecoder/pricing.json` 已补 `mimo-v2.6-flash` | 预算/成本类优化有了输入 |

重构后这三条的状态各自动了一次，v1.3 / v1.5 又各动了一次：

- 缺陷 1 **已修（v1.3）**：conftest 的 autouse fixture 把 `CORECODER_AUDIT_FILE` 指到 `tmp_path`、
  `CORECODER_TRACE` 置 `off`。这里只改路径是挡不住的，因为重构前这条曾更严：`.env` 里
  `CORECODER_TRACE_CONTENT=true` 常开，测试漏出去的是完整的提示词与工具 I/O
- 缺陷 2 **已修（v1.5）**：`_log_run_summary` 多传一个 `error_type=run.error_type`，跟 `cost_usd` 用同一个
  空值写法（没出错就是 `error_type=None`）。v1.4 之前它是「577 条里 0 条带错误类型」；
  修完后第一次真实跑就用上了：一次被外部管道提前关掉输出导致的失败，现在记成
  `outcome=error error_type=OSError`，而不是一个只知道「失败」的 `error`
- 缺陷 3 **已修（v1.5）**：单价来自官方价目页（2026-10-21 更新），实时推理、非缓存输入
  `$0.14 / $0.28` 每百万 token（国内等价 ¥1 / ¥2）。反算核过：`tokens_in=10797 tokens_out=11`
  对应 `cost_usd=0.001515`。**口径要记住**：`LLM.estimated_cost` 不区分缓存命中 token，
  而 agent 负载的命中率极高（每轮重复带系统提示词与工具 schema），所以这个数是**上界**，
  真实账单只会比它低

### 3.3 观测数据自身的三条限制

1. **保留上限是配置项，不再是写死的 10**（v1.7）。`audit.prune_old_logs`（原
   `observability.prune_old_logs`，已改名）的默认值提成了模块常量 `_DEFAULT_KEEP = 10`，
   实际取值走 `CORECODER_AUDIT_KEEP`；不设它就等于旧行为，本仓库 `.env` 设的是 500。
   机制细节仍然成立：prune 只动**默认路径**——`audit._open_audit_file` 里那句 `prune_old_logs(...)`
   被 `if explicit is None:` 包着。作者的理由写在注释里：默认文件名带 pid，一次进程一个，
   而 100MB 的按大小轮换对几 KB 的小文件几乎不触发，只能靠数个数兜住。
   **不要改回「显式指定一个固定路径」来归档**（v1.6 考虑过、v1.7 放弃）：它有两笔代价——
   ①大文件轮换照旧生效，超 `CORECODER_AUDIT_MAX_MB`（默认 100）会 `os.replace` 成 `.1` 且只留一份，
   不是「永不删」；②默认路径带 pid 本来就是为了**进程隔离**，指一个固定路径等于放弃它，
   两个终端同时往一个 fd 追加，Windows 上跨进程交错没有保证。现在没必要付这两笔钱。
   容量：实测约每天 0.5–1MB（今天 11 个文件共 566608 字节），`keep=500` 封顶也就 50MB 量级。
   `tracing.py` 的 JSONL 分支仍是写死的 `keep=10`——当前走 langfuse 后端，等真用 jsonl 归档再统一。
2. **只记元数据**。审计流按设计不含工具参数、结果与用户输入，所以它只能回答「谁、何时、多久、花了多少」，
   回答不了「它为什么这么干」。后者要看 trace（`CORECODER_TRACE_CONTENT` 为真才记录内容；
   现在是常开，而且 `tracing.py` 自己就警告过「redaction is best-effort」）。
3. **trace 与 audit 是两套开关**。`CORECODER_TRACE=langfuse` 时失败会退到 `NullTracer`（不降级写文件），
   所以「有 audit 无 trace」是正常状态，不要当成丢数据。重构后多了一条：tracing 关时
   `current_trace_context()` 返回 `None`，audit 行就**不带** `trace_id`（实测 2780 行里 219 行如此）。
4. **一个用户回合可能对应两行 `run_summary`**（v1.8 随 B5 带回）。`AgentTool.execute` 现在把父的
   `session_id` 传给子 agent，于是子 agent 自己的 `run_summary` 也落在同一个会话里。按会话
   数「跑了几次」不再等于「用户问了几句」。更要当心的是一处**结构性重复计**：子 agent 与父共用
   同一个 `llm` 对象，而 `run_summary` 的 `cost_usd` 是 `_cost_delta(run.cost_before, 当前累计)`，
   子那行的增量是父那行增量的**子集**——把某会话的 `run_summary` 按行相加，成本会高于真实账单。
   没有子 agent 时不受影响（只有一个父 delta），所以现在读数为 0 差异；**按会话算成本请改用
   `llm_call` 行**，它一次模型调用一行，结构上不会重。要不要给子 agent 的运行汇总打个标记
   （如 `sub=true`）以便按行剔除，等阶段 1 真出现带子 agent 的成本统计再定。

### 3.4 Langfuse 侧实测（主体 v1.2；标 ✓ 的四行为 v1.8 修复后实测）

审计流的问题在 3.2 已经列过，trace 流是另一套账。下面是直接查本地 Langfuse
（REST `/api/public/traces`、`/api/public/observations`、`/api/public/scores`）得到的读数，不是推测：

| 指标 | v1.2 实测值 | 含义 |
|---|---|---|
| trace 总量 | 2813（v1.1 时 205、v1.2 时 2703） | 增长几乎全由测试跑批贡献；**v1.3 后测试不再往里写**（v1.7 实测 2814） |
| 名称分布 | 最新 1000 条里 `agent.chat` 848、`tool.*` 152 | **15.2% 的 trace 其实是一个工具 span 自己**（见 3.5 的 N3） |
| observation 类型 | GENERATION 4006 / TOOL 2438 / SPAN 2336 / EVENT 753 / AGENT 0 | span 形状已对；AGENT 为 0 → 第 9 项 |
| `environment` | 抽样 1000 条全为 `default`（历史数据） | **v1.3 已修**：最新一条真实运行实测 `environment=dogfood`，旧数据仍集中在 `default` |
| `tags` / `userId` | ✓ 新运行 `tags=['build:probe']`、`userId=synapse` | **v1.8 已修（B8）**：`Agent._labels` 取值随 root 传；历史仍空 |
| `scores` | 0 | B7 未做；SDK 已提供 `observation.score()` / `score_trace()`（v1.8 实测确认签名） |
| observation `level` | ✓ 失败运行的 `tool.boom` 与 root 都 `level=ERROR`，带 `statusMessage` | **v1.8 已修（B1）**；历史仍全 `DEFAULT` |
| `trace.output` | ✓ 正常运行有值；异常上抛的仍 `None`，由 `level=ERROR` 说明 | **v1.8 已修（B4）**：`chat()` 返回值交给 root；正文按轮仍在 `llm.round` |
| `totalCost` | 1000 抽样里 13 条非零 | **已改善**：原生 `cost_details` 已写（非零的都是价目表里有价的模型）；`mimo` 仍空 → B3 只剩价目表条目 |
| session | `agent.chat` 848 条都有 `sessionId`；152 条孤儿全空 | 会话维度可用（v1.8 实测：子 agent 与父同会话，不再是第二个会话） |

结论：**span 结构与原生字段都已到位**（generation / tool / event 三种形状都落库，usage 与 cost 走原生通道），
分类维度与失败标记在 v1.8 补上，只剩 B7（scores）；曾经更基本的 join key 问题（3.5 的 N2）也已闭合。

### 3.5 重构带回的四个问题（v1.2 新增）

按严重度排序。N1 是代码缺陷不是观测装饰，因为它直接打在阶段 1 要读的三个数上。

**N1　`outcome` 会把崩溃标成「轮数用尽」**——**v1.4 已修**

- 证据：10 个 audit 文件里 12 条 `run_summary` 是 `outcome=max_rounds rounds=1`，而同一个 `trace_id` 下
  **没有** `event=max_rounds` 行；紧前面一行是 `tool_decision tool=exiter`（一个抛 `SystemExit` 的测试工具）。
- 原因：`_RunStats.outcome` 的默认值就是 `"max_rounds"`（靠「循环耗尽时没人改写」生效），而 `Agent.chat`
  只捕获 `(Exception, KeyboardInterrupt)`。`SystemExit`、`GeneratorExit` 这类 `BaseException` 穿过 `except`，
  但 `finally` 照样跑 `_finish_run` → 记成「轮数用尽」，`error_type` 为空。
- 影响面：`run_summary` 的 `outcome` 与 root span 的 `outcome` metadata 同源，两边一起错。
- 已落地的修法（见 0C 的 C1）：默认值换成中性哨兵、耗尽分支显式标注、`except` 改接 `BaseException`。
  为什么测试没发现它：旧用例只断言 `SystemExit` 能传播与历史被回填（`tests/test_agent_loop_hardening.py`
  的 `exiter` 用例），不断言标签。

**N2　audit 的 `trace_id` / `span_id` 在 Langfuse 里查不到**——**v1.8 已修（方向与原文相反，见 0C 的 C2）**

- 证据：audit 里共 531 个 `trace_id`，与 Langfuse 最新 100 条 trace id **交集为 0**；拿 audit 的 id 去查
  `/api/public/traces/<id>` 一律 404。
- 原因：`_SpanTracer._origin` 用 `uuid.uuid4().hex` 自造 trace id，Langfuse 用 OTel 生成的 id，两边从不对齐。
  `tracing.py` 与 `audit.py` 的模块 docstring 都写着「一行和它的 trace 记录可以 join」，这句话对
  jsonl 成立、对 langfuse 不成立。
- 修复前记在这里的「可修的点」（`start_as_current_observation(trace_context=...)`）**不是最终落地的路子**：
  实测 SDK 的 observation **公开** `trace_id` 与 `id` 两个属性，所以不需要 `getattr` 进 OTel 内部，
  也不必让 SDK 接受我们的 id——反过来采用后端的 id 更便宜。v1.8 复算：拿审计行的 `trace_id` 查
  `/api/public/traces/<id>` 返回 200，且 `span_id` 与 observation 的 `id` 同为 16 位 hex（自造 id 是 32 位，
  长度差本身就是采用成功的信号）。

**N3　没有 run 上下文时，工具 span 自己成了一条顶层 trace**

- 证据：最新 1000 条里 152 条顶层 trace 的名字是 `tool.*`，它们的 `sessionId` 全空；全部来自
  `tests/test_audit.py` 直接调 `agent._execute_round(...)`（不经 `chat()`，所以没有 root span）。
- 今天生产路径踩不到（`start_run` 只在 `chat()` 里调），但结构上没有任何防护：`RunTrace.tool_span` 在
  `_origin` 找不到 parent 时静默开一条新 trace，session 也继承不到。
- 处置：**测试侧已由 v1.3 的 0-1 堵住**（跑完 `pytest`，`tool.*` 顶层 trace 零新增），
  152 条存量孤儿不会自己消失；要不要在 `RunTrace` 里加「无 root 就不开 span」的判据，
  等阶段 1 真出现孤儿再定（见第 6 节）。

**N4　`retime()` 只对 jsonl 后端生效**——作者在 docstring 里写明是刻意限定，所以不列为待修，
已归入第 5 节观察项（第 11 项）。

## 4. 执行计划

### 阶段 0：让数据能读（约 1 天）

阶段 0 分四段：0A 只动配置与测试隔离，0B 修观测数据的分类与标记，0C 修重构带回的三个归因问题，
0D 把 audit 的保留上限做成配置项。下面四条记为 0A。

前置条件：基线绿（`pytest tests/ -q`、`ruff check corecoder tests`、`python -m compileall -q corecoder tests`）。

| # | 动作 | 落点 | 验收标准 | 状态 |
|---|---|---|---|---|
| 0-1 | 隔离测试的观测落盘 | `tests/conftest.py` 新增 autouse fixture | `pytest` 前后 `~/.corecoder` 字节数不变，且 Langfuse 无新增 | **v1.3 ✅** |
| 0-2 | 给真实运行打标 | `.env` 增加 `CORECODER_BUILD=dogfood` | 新的 `run_summary` 里 `build=dogfood`（见附录 B） | **v1.3 ✅** |
| 0-3 | 补价目表条目 | `~/.corecoder/pricing.json` 增加 `mimo-v2.6-flash` 单价 | `run_summary` 的 `cost_usd` 从 `None` 变为数字 | **v1.5 ✅** |
| 0-4 | 放开输出上限 | `.env` 增加 `CORECODER_MAX_TOKENS`，按 provider 上限设 | `output_truncated` 下降，不再撞 `TruncatedOutput` | **v1.3 ✅** |

v1.3 的实测验收（只动 `tests/conftest.py` 与 `.env`，`corecoder/` 一行未改）：

- 0-1：跑完整一遍 `pytest`（351 passed / 1 skipped，与改前一致）后，`~/.corecoder` 的
  audit 共 10 个文件、809652 字节 → **零增长**；Langfuse trace 2811 → **零增长**（之前每跑一次涨约百条）
- 0-2：真实运行的 `run_summary` 实测包 `build=dogfood`、`session=session_20261026_185049_e66108b5`
- 0-4：上限不是猜的 —— 服务商的 400 原话是「at most **131072** completion tokens」，
  取了保守的 `16384`（留出提示词空间，也避开与 `max_context_tokens=128000` 撞车）
- 复算入口：`.tmp` 下的只读探针不入库，等价命令在附录 A.1（信噪比）与 A.7（REST 读数）
- 补测（`.env` 加完上述三行之后）：重跑 `pytest`，351 passed / 1 skipped 不变，
  audit 790611 → 790611 字节、trace 2812 → 2812 条，零增长依旧成立
  （字节数比上一条低是因为 prune 已裁旧文件，见 3.1 的漂移说明）

0-1 的实现骨架：

```python
@pytest.fixture(autouse=True)
def _observability_isolated(monkeypatch, tmp_path):
    """Audit and trace must never reach the real ~/.corecoder or Langfuse."""
    from corecoder import audit
    monkeypatch.setenv("CORECODER_AUDIT_FILE", str(tmp_path / "audit.log"))
    monkeypatch.setenv("CORECODER_TRACE_FILE", str(tmp_path / "traces.jsonl"))
    # the .env value is "langfuse": leaving it on really connects to the local
    # instance, and CORECODER_TRACE_FILE does not apply to that backend
    monkeypatch.setenv("CORECODER_TRACE", "off")
    monkeypatch.setenv("LANGFUSE_TRACING_ENVIRONMENT", "test")
    audit.reset_audit()
    yield
    audit.reset_audit()
```

五个实施注意点：

- **不要**在 conftest 里把 `CORECODER_AUDIT` 直接关掉。`tests/test_bad_argument_diagnostics.py`
  依赖 audit 开关为真才能断言审计流；只改**路径**最安全。
- `CORECODER_AUDIT_FILE` 的显式传参优先于环境变量，所以 `tests/test_audit.py` 自带的
  `audit_env` fixture 不会被 0-1 影响。
- **还要挡一个新 sink**。重构后 `audit._build_handler` 多了一档：`CORECODER_AUDIT_STREAM=stderr`
  优先于文件。它默认不设，但一旦有人设了，上面的 `CORECODER_AUDIT_FILE` 就拦不住任何东西；
  要么在 fixture 里一并 `monkeypatch.delenv("CORECODER_AUDIT_STREAM", raising=False)`，要么当成约定写下来。
- `CORECODER_TRACE_FILE` 只在 `CORECODER_TRACE=jsonl` 时被读取（`create_tracer` 的 jsonl 分支）；
  backend 为 `langfuse` 而 SDK 不可用时直接退到 `NullTracer`，不会改写到这个路径。
- **langfuse 后端根本不会读 `CORECODER_TRACE_FILE`**，所以只改路径关不住它。而且现在
  `CORECODER_TRACE_CONTENT=true` 常开，漏出去的不仅是元数据，是完整的提示词与工具 I/O。
  实测证据：本仓库跑一次 `pytest`，Langfuse 最新 1000 条 trace 几乎全是测试产物（`tool.writer`、
  `tool.boom`、`tool.exiter` 这些只存在于测试里的工具名都在）。
  原因是多数测试构造 `Agent(...)` 时不传 `tracer`，于是走 `create_tracer()` 继承 `.env` 的
  `CORECODER_TRACE=langfuse`——必须在 conftest 里显式置 `off`。

#### 阶段 0B：把观测数据修成可筛选的形态（半天）

0A 的四条解决「数据别写错地方」，0B 解决「数据写对了但筛不动」。
编号与讨论清单一一对应，便于追溯；v1.2 在每条后面标了复验状态。

| # | 问题 | 落点（符号） | 改法 | 验收 / v1.2 复验状态 |
|---|---|---|---|---|
| B1 | 失败无标记 | `_LangfuseSpan._close` | `update(level="ERROR", status_message=self.error)` | **v1.8 已修**：span 与 root 都标 `ERROR` |
| B2 | 测试混池 | `.env` | 设 `LANGFUSE_TRACING_ENVIRONMENT=dogfood` | **v1.3 已修**：最新真实运行实测 `environment=dogfood` |
| B3 | 成本恒 0 | 同 0-3 | 补 `~/.corecoder/pricing.json` | **v1.5 已修**：价目表条目已补，真实运行实测 `cost_usd=0.001515` |
| B4 | trace 层无答案 | `_finish_run` | 给 root 记 `output`（即 `chat()` 的返回值） | **v1.8 已修**：`_RunStats.answer` 写进 root，实测有值 |
| B5 | 子 agent 无视注入的 tracer | `AgentTool.execute` | 子 `Agent(...)` 传 `tracer` 与 `session_id` | **v1.8 已修**：两行参数；症状原记反了，见下方约束 |
| B6 | 重试只在 audit | `LLM.chat` | 加事件回调，`Agent` 传入 span 的 `event` | **已降级**：已有 join key（v1.8 后两边同一个 id），只差 trace 里的 event |
| B7 | 没有 score | `Span`、`_LangfuseSpan` | root 结束时按 `outcome` 打分 | **未修（v1.8 推后）**：scores 仍 0；`score_trace()` 实测可用，缺「拿什么判好坏」 |
| B8 | tags / userId 全空 | `Agent._labels` → `start_run` | 以 tags/user_id 传入 | **v1.8 已修**：tag 带 `build:` 前缀；新增 `CORECODER_USER` |

八条实施约束，都是实测踩出来的，不要绕过：

- **B1 的 error 现在被吞掉**：`_BaseSpan.end` 把 `error` 从 attrs 里 pop 进 `self.error`，
  `_JsonlSpan._close` 会写它，`_LangfuseSpan._close` 只 `_apply(self.attrs)`，从不设 level。
  这是两个后端行为不一致，不是 Langfuse 的限制——实测 `level="ERROR"` + `status_message` 能正常落库。
  v1.2 再补一刀：因为 `error` 已被 pop 出 attrs，它在 Langfuse 连 metadata 都进不了（实测 root span
  metadata 只有 `version`/`max_rounds`/`outcome`/`rounds`/`tool_calls`/`tools_executed`/`tokens_in`/
  `tokens_out` 加两个 OTel 内部键），信息是**彻底丢了**，不只是归错位置。
  另外 SDK 没有 `set_level()`，只有 `update(level=..., status_message=...)`，别往那个方向试。
  **v1.6 复核：传入链路已经通了**，只剩消费端。`Agent._finish_run` 在 `run.error_type` 非空时写
  `fields["error"]`（v1.5 随缺陷 2 接上的），`RunTrace.tool_failed` 也早就在写 `span.end(error=...)`，
  两者都被 `_BaseSpan.end` 收进 `self.error`。所以 B1 剩下的不是数据流改造，是 `_LangfuseSpan._close`
  里加两行：`self.error` 非空时 `update(level="ERROR", status_message=self.error)`。
  **v1.8 已按此落地**，一字未改：`_close` 在 `_apply(self.attrs)` 之后补一次 `update`，
  不往 `_apply` 里塞（那样 `level` 会被当成 metadata）。成功的路径不设 `level`，免把正常运行标高。
- **B2 的环境变量必须在 `get_client()` 之前设置**。`create_tracer` 里的 `LangfuseTracer`
  首次构造时才调 `get_client()`，所以 `.env`（经 `Config._load_dotenv`）与 conftest 的
  `monkeypatch.setenv` 都赶得上；同一进程内第二次修改就不再生效。
  重构后多了一个选项：`propagate_attributes` 本身带 `environment=` 形参，若走 B8 的
  `start_run` 透传路线，就不必依赖进程级变量（但那样一来环境由代码定，不再由 `.env` 定）。
  这条路不免费：corecoder 的 `_trace_attributes` 只映射 `session_id` / `user_id` / `tags`，
  要透环境先得给这个助手加一个形参。
- **B4 缺的是「这次交付了什么」，不是「正文全没了」**（v1.6 按重构后的代码改写此条）。一次 `--yes`
  真实运行的 REST 读数：`llm.round` 那条 GENERATION 的 `output` 是 `'OK'`，而根 span `agent.chat`
  与 trace 层的 `output` 都是空。原因在 `Agent._model_round`：重构后它每轮都调
  `call.capture_output(resp.content or "")`，所以正文**按轮**记在 generation 上；`end_run` 只往下递
  `outcome`/`rounds`/`tokens`/`cost`，从不递文本。三个具体缺口：
  ①列表页那一栏看不出交付了什么，得点进 trace；②多轮时要自己判断哪一轮是最后一轮；
  ③`chat()` 的兜底返回值（`(reached maximum tool-call rounds)`、`[interrupted]` 回填、`TruncatedOutput`）
  根本不是 `resp.content`，从来没进过任何 span——而这三个恰恰是最需要回看现场。
  另记住它挂在开关上：`llm.round` 的 `output` 依赖 `CORECODER_TRACE_CONTENT`，关掉后
  `_filter` 剥掉 `("input", "output")`，trace 里就一个字的正文都不剩。
  改法：把返回值交给一个局部变量，`_finish_run` 用它写 root 的 `output`（只在 `capture_content`
  为真时写，与 `capture_request` 一致）。重构后这一步反而容易了：`chat()` 现在只有一处
  `return self._run_rounds(...)`，接住它比在多个中途 return 前赋值干净。
  **v1.8 已落地，但本节上面那句「三个兜底返回值」写错了，修正如下**：`chat()` 只有**两个**正常
  返回——`resp.content` 与 `(reached maximum tool-call rounds)`；`TruncatedOutput` 是 `raise`，
  `[interrupted]` 是往历史里回填工具回复、不构成 `chat()` 的返回值（见 AGENTS.md 1.1 那三条兜底分支
  的形状）。所以落地面是两个返回都记（`_RunStats.answer` 只在正常返回时赋值），`max_rounds`
  那条占位文本现在与用户看到的一致；抛异常的运行 `trace.output` 保为 `None`，由 B1 的
  `level=ERROR` + `statusMessage` 说清原因。它天然挂在 `CORECODER_TRACE_CONTENT` 下：关掉后
  `_filter` 剥掉 `output`，跟本轮没记一样。没有把正文也放进 `run_summary`：审计行按设计只记
  元数据（见 3.3 第 2 条），而答案可以无限长，进审计行等于绕过 `_govern_result` 的钳制。
- **B5 不能只传 tracer**（v1.8 已修：`tracer=parent.tracer` 与 `session_id=parent.session_id` 两行一起加）。
  先把症状改对：本节原记「子 agent 断链／无 trace」，**实测是反的**——子 agent 一直有 trace，
  而且在 Langfuse 里确实挂在父 trace 下面（OTel 上下文自己就传过去了）。真问题有三个，
  都不是「看不到」：①它自己调 `create_tracer()`，**无视调用方注入的 tracer**——实测父用 jsonl 后端时，
  子 agent 把整棵树传进了 Langfuse，众目睽睽下多一条谁都没要的孤儿 trace；②jsonl 后端下父子是
  两棵断开的树；③子 agent 自造 session id，一次提问在 Sessions 视图里变成两个会话。传了这两个
  参数三条一起消失：实测子 `agent.chat` 的 `parentObservationId` 正是父的 `tool.agent`，两条运行同一个
  `sessionId`，且不再多出一条 trace。原本写的「B5 与 C2 要一起改」成立了，两边确实同一轮落地。
  本轮没修的连带事项：子 agent 的 root 名仍与父同为 `agent.chat`（建议 `agent.sub`），与第 9 项的
  `as_type` 一起规划；共享 session 后 `run_summary` 每回合可能两行，成本子集问题另见 3.3 第 4 条。
- **B6 走回调，不改签名**：`llm_retry` 与 `provider_fallback` 都在 `LLM.chat` / `LLM._open_stream` 内部产生，
  `Agent` 拿不到。与其让 `LLM` 认识 tracer，不如给它一个可选的事件回调。
  v1.2 重新定级：这两类审计行现在已经带上 ambient span 的 `trace_id`/`span_id`（已用 jsonl 后端
  离线实验证实），归因问题已解决，剩下的只是「Langfuse 时间线上看不到重试」。排到 B7 之后。
  （注意：测试里那 24 条 `llm_retry` 全是零 join key，因为它们直接驱动 `LLM`/`LiteLLM`，根本没有 span。）
- **B7 不能用 `self.trace_id` 去 `create_score`**——**前提已在 v1.8 消失**：`_BaseSpan.trace_id` 是 corecoder
  自己生成的 uuid、与 Langfuse 不同值这件事，在 C2 落地后不再成立（Langfuse 后端下 `self.trace_id`
  **就是**后端的 trace id），所以 `create_score(traceId=...)` 这条路现在可用了。保留的技术事实：
  更稳的两条不涉 id——root observation 自己的 `observation.score(name=, value=)`，或
  `score_current_trace`（必须在 root span 上下文未退出时调，写进 `_LangfuseSpan._close` 最稳）。
  v1.8 实测 SDK 5.0.4：`update()` 的签名公开带 `level` 与 `status_message`，`score` /
  `score_trace` / `create_score` / `create_trace_id` / `get_current_trace_id` 全在公开面上；
  推后它的理由不是 API，是**没有判断来源**：`outcome=answer` 不等于答对了，凭 `outcome` 打分
  只会造出一堆自我肯定的 score，反而把 B7 想做的评价面板先玩坏。
- **B8 的 tags 创建后不可变**：Langfuse 的 trace tags 是创建时定死的，后补无效；必须随 root span 一起传。
  代码里已经有了这个限制的依据：`LangfuseTracer._open_trace_attributes` 在 `origin.parent` 非空时
  直接返回 `None`，子 span 上的 `tags`/`user_id` 只会落成普通 metadata。
  `RunTrace.start_run` 曾是唯一没把 `tags`/`user_id` 往下传的环节（v1.6 核对签名确实没有这两个形参）。
  **v1.8 落地形态与两条原写法不同，理由记在这**：①取值放在 `Agent._labels`（`chat()` 调 `start_run` 时
  `**` 展开），而不是在 `tracing.py` 里 `os.getenv`——附录 B 早就警告过拆两个模块各读一半；
  `Agent._log_run_summary` 已经在读 `CORECODER_BUILD`，标签归位同一个模块才不会出现「改了 audit
  忘了 trace」；`RunTrace.start_run` 只多两个透传形参（空值不往 `tracer.start` 里塞，免为 jsonl
  记录添 `null` 噪声）。②tag 带前缀 `build:`，因为 Langfuse 的 tag 列表里一个光溜的 `dogfood`
  与任意其他标签不可区分。③新增只读开关 `CORECODER_USER`（不设就是 `userId=None`）：姓名是
  个人数据，只允许显式提供。**副作用要记住**：conftest 必须同时 `delenv` 这两个变量，否则
  `.env` 里的 `CORECODER_BUILD=dogfood` 会跟着某次 `Config.from_env()` 进进程环境，使
  「没打标」那一类断言依赖测试执行顺序（v1.7 在 `CORECODER_AUDIT_KEEP` 上踩过一次同样的坑）。

完成 0B 后，阶段 1 的读数入口从「翻 audit 日志」变成「Langfuse 里按 environment + level + tags 筛」。
**v1.8 起这句话成立**（只剩 `scores` 那一档还没数据）。

#### 阶段 0C：先修重构带回的三个归因问题（约两小时）

这一组不是「让数据更好筛」，而是「数据在说谎」。C1 优先于整个 B 组，因为它污染的就是
阶段 1 要读的 `outcome`。三条都带证据编号，细节回 3.5 看，这里只写改法与验收。
**v1.8：三条全部落地。**

- **C1　`outcome` 把崩溃标成「轮数用尽」**（3.5 的 N1）——**v1.4 已修**
  - 落点：`_RunStats.outcome` 默认值、`Agent.chat` 的 `except`、`_run_rounds` 的耗尽分支
  - 改法：默认值换成中性哨兵（`"error"`）；循环真耗尽时显式写 `max_rounds`；`except` 改接 `BaseException`，
    记完标签原样 `raise`
  - 验收（已逐条跑过）：`tests/test_audit.py` 的 `test_run_summary_never_files_a_crash_as_round_exhaustion`
    先失败后通过——把 `agent.py` 临时撤回修复前，它读出 `AssertionError: max_rounds`，即 bug 本身；
    修复后真耗尽仍为 `max_rounds` 且有 `event=max_rounds` 行佐证，抛 `SystemExit` 的那次记为 `error`
    且**没有** `max_rounds` 行；全量 353 passed / 1 skipped，compileall 与 ruff 均绿
- **C2　audit 的 id 在 Langfuse 里查不到**（3.5 的 N2）——**v1.8 已修，但走了与本文相反的方向**
  - 本文原方案：root span 往 `start_as_current_observation(trace_context={"trace_id": …})` 里塞我们的 id，
    让 SDK 接受 corecoder 的 id；第 6 节还写过「反过来回写 SDK 的 id 要一路 `getattr` 进 OTel 内部，
    等于把可观测层做成硬依赖，所以不做」。**那句判断是错的**：实测 SDK 5.0.4 的 observation
    **公开** `trace_id`（32 位 hex）与 `id`（16 位 hex）两个属性，根本不需要 `getattr` 探内部对象
  - 落地方案（`_LangfuseSpan.__init__`）：observation 建好后把 `self.trace_id` / `self.span_id` 换成
    后端公开的那两个值，`current_trace_context()` 一字不改，审计行自动拿到可查 id；SDK 不公开
    id 时（旧版或替身客户端）保留自造 id，不依赖它存在
  - 为什么选这个方向：它是**读取已有事实**，原方案是**让另一个系统接受你的 id**——后者要处理
    id 合法性与 SDK 行为差异，且一旦 SDK 不采纳就会静默失败；前者失败就是回退到自己造 id
  - 代价（要认）：同一段代码在 jsonl 与 langfuse 两个后端下产生**不同形状的 id 空间**（32 位 vs
    16 位 span_id），跳后端比对时不能拿 id 字符串直接等；`audit.py` 里「一行和它的 trace 记录可以
    join」这句 docstring 因此对两个后端都成立
  - 验收（已跑）：拿审计行的 `trace_id` 命中 `/api/public/traces/<id>` 返回 200（命令见 A.7）；
    离线用 `tests/test_tracing_langfuse_v5.py` 的两条新用例覆盖（采用后端 id / SDK 不公开展示时回退）
- **C3　测试直接驱动内部方法造孤儿**（3.5 的 N3）——**v1.3 已随 0-1 落地**
  - 落点：`tests/test_audit.py` 的 `_execute_round` 用法 + 0-1 隔离
  - 改法：靠 0-1 把 trace 关掉；先不改 `RunTrace` 的形状
  - 验收：跑完 `pytest`，Langfuse 里 `tool.*` 顶层 trace 不再新增（实测已零新增）

C1 只改归因，不改循环形状（`chat()` 的四段骨架与三条兜底分支都不动）；`_outcome_of` 本身已经接受
`BaseException`，只是 `except` 子句没把非 `Exception` 的那类递给它。已落地（v1.4）。
C2 选「让 Langfuse 用 corecoder 的 id」，而不是反过来在 span 创建后把 Langfuse 的 id 回写 audit——
后者需要一路 `getattr` 到 SDK 内部的 OTel span，把一个可选的观测层做成硬依赖。

#### 阶段 0D：让 audit 留得住历史（已落地，v1.7，约十分钟）

起因：阶段 1 要跑 1–2 周，而实测发现保留窗口连一天都不够——`~/.corecoder` 里的 10 个 audit 文件
时间戳**全是当天的**（10 个不同 pid）。`keep=10` 数的不是运行次数也不是天数，是**进程数**。

| 项 | 内容 |
|---|---|
| 落点 | `corecoder/audit.py` 的 `_open_audit_file` 一处调用 + 新增模块常量 `_DEFAULT_KEEP` |
| 改法 | `keep=_env_int("CORECODER_AUDIT_KEEP", _DEFAULT_KEEP)`；默认值不变，没设过的人行为不变 |
| 配置 | `.env` 加 `CORECODER_AUDIT_KEEP=500` |
| 隔离 | `tests/conftest.py` 的 `_observability_isolated` 加一行 `delenv`，否则旧断言会被开发者的 500 弄红 |

三条验收，都是实跑的：

- 单元测试 `test_prune_count_is_configurable_and_a_bad_value_falls_back`：先失败
  （`assert 7 == 3`，设了 KEEP 但一个没剪），改实现后通过；同时钉住「非数字退回 10」与
  「每次 configure 重读 env，不是 import 时缓存」
- 真实路径等价探针：`Config.from_env()` 后触发首次 `log_event`，audit 文件数 **10 → 11**
  （`keep_read 500`）——对比未生效时是 10 → 10
- 隔离复验：带着新 `.env` 跑完整 `pytest`，354 passed / 1 skipped，`~/.corecoder` 的
  audit 11 文件 / 566608 字节、Langfuse 2814 条，**零增长**

**一个别踩的坑：`--demo` 不加载 `.env`。** `cli.py` 的 demo 分支在 `config = Config.from_env()`
**之前**就 `raise SystemExit(run_demo())`，所以 demo 进程里的 audit 永远按内置 10 剪——第一次拿
`--demo` 做验收就是被这条骗的，看到 10 → 10 以为实现无效。
**不要去「修」这个顺序**：demo 一旦继承 `.env`，就会把 `CORECODER_TRACE=langfuse` 一起继承，
离线演示会往真实 Langfuse 写——正是 0-1 刚消灭的那类污染。demo 本就该只看 shell 环境。

**对阶段 1 的意义：不需要任何归档动作。** 不改路径、不搬文件、不设计划任务；
附录 A.1 / A.2 / A.5 本来就是 `glob('~/.corecoder/audit.*.log')`，上限一提自动变成全历史统计。

### 阶段 1：只跑不改（1–2 周）

用 CoreCoder 开发一个**本来就要做的**真实项目。不要为了测而测——没有真实目标时，人会不自觉绕开它的弱点。

**纪律**：本阶段不改 `corecoder/` 下任何代码。否则「它能力不行」和「我刚改坏了」无法区分。
阶段 0B 与 0C 必须在进入本阶段**之前**做完——它们改的全是 `corecoder/` 下的文件，带进阶段 1 就破坏了这条纪律。

每次会话结束后看三个数（都在 `run_summary` 里现成）：

| 指标 | 怎么读 | 异常信号 |
|---|---|---|
| `rounds` | 本次经过几轮工具调用 | 普遍 >30 说明提示词或工具描述有歧义 |
| `outcome` | `answer` `error` `truncated` `max_rounds` `interrupted` | `truncated` 多因 0-4 没做；`max_rounds` 到 v1.4 才可信 |
| `tokens_in` | 累计输入 token | 涨得比 `tokens_out` 快得多，说明上下文在重复付费 |

这三行读的是 `run_summary`。重构后另有一条捷径：审计行已经带上当时打开的 span 的 id
（`trace_id`/`span_id`），所以一行 audit 能直接指到一条 trace。v1.8 之前这句只对 jsonl 后端成立；
**现在两个后端都成立**，Langfuse 侧的 id 已在 C2 里对齐（实测拿审计行的 id 查 REST 返回 200）。

还有一条 v1.8 起要当心的口径：上面三行是按 `run_summary` 读的，而一个用户回合现在可能有两行
（子 agent 与父同会话、成本增量是父的子集），详见 3.3 第 4 条。

每次「卡住 / 改错 / 让我不耐烦」记一行：**现象 + 它当时调了什么工具 + 当时的 `outcome`**。

**验收**：`build=dogfood` 的会话 ≥20 次，且能给出非 `answer` 的具体占比。

#### v1.3 验收跑顺手留下的第一条记录

一句 `reply with exactly: ok`（`--yes`、单轮、无工具调用）的实测 `run_summary`：
`outcome=answer rounds=1 tool_calls=0 tokens_in=10794 tokens_out=1323 duration_ms=40683 cost_usd=None`。

三个读数各自指向什么：

- **10794 的固定底数**：一次空任务就要带着系统提示词、9 个内置工具的 schema，外加这个环境里
  已连上的 MCP filesystem 工具的 schema（`Agent._fixed_prompt_tokens` 算的就是这一块）。
  真实任务的性价比得按这个底数算，不是按「我以为一句提示词多少钱」算
- **`cost_usd=None` 在生产路径上又复现了一次**：这就是 3.2 缺陷 3 的具体样子，直接卡住
  fallback + 预算闸那一项（因为它需要的就是成本读数）
- **1323 个输出 token 答了一个「ok」**：模型把指令理解成了「改写自己的思考过程」。**不要把这条
  归咎于 0-4**：1323 远低于旧默认的 4096，换成旧值它不会变好，只会被截断。这就是阶段 1
  要收集的那类现象：模型在真实提示词下的形状问题，而不是参数问题

#### v1.5 验收跑留下的第二、三条记录（成本与错误类型首次有读数）

同一个提示词换成更硬的措辞后，连着两次真实运行（`session_20261026_190915_2388146b` 与
`session_20261026_190930_4d62011b`）：

- 成功的那条：`outcome=answer rounds=1 tool_calls=0 tokens_in=10797 tokens_out=11`、
  `cost_usd=0.001515 duration_ms=2610`。模型这次只用了 11 个输出 token 就答对了——上一轮的 1323 是措辞
  问题而非能力下限，这条对比本身就是阶段 1 要收集的东西
- 失败的那条：`outcome=error error_type=OSError tokens_in=0 cost_usd=0.0 duration_ms=1794`——
  原因是我在 shell 里把输出接到了一个写坏的管道上，不是 CoreCoder 的错；但意义就在这里：
  缺陷 2 修前它只会是一条 `outcome=error`，现在能直接读到 `OSError`，把归因指向 shell 而不是工具链
- `cost_usd` 从 `None` 变成 `0.001515`：0-3 落地的验收就是这个数字，且反算与单价完全吻合
- 固定底数仍在那：`tokens_in=10797`，与上一轮的 10794 只差 3 个 token——这就是每轮都要重付的
  系统提示词 + 9 个工具 schema + MCP filesystem schema（也是为什么缓存命中价对 agent 负载那么关键）

### 阶段 2：把现象固化成用例（1 周）

把阶段 1 里出现频率最高的现象，每条写成一个可稳定复现的脚本，放 `examples/`，
照 `examples/m2_truncated_tool_call.py` 的样子（先钉住 bug，再改代码）。

这就是 L2 评测集的实际起点：**用例从真实痛点长出来，而不是先建一批再找地方用。**

**验收**：至少 3 条脚本，能在未修复的代码上稳定复现同一现象。

### 阶段 3：按证据排序做优化

拿阶段 2 的用例当尺子，出现频率最高的先修。

| 待做项 | 何时可开工 | 开工前提 |
|---|---|---|
| bash 真沙箱 | 随时 | 无。它是评测基础设施的前置件：没有隔离，就不该让 agent 在陌生仓库里跑测试 |
| fallback + 美元预算闸 | 阶段 0 之后 | 需要 `cost_usd` 有读数 |
| 并发区分读/写 | 阶段 2 之后 | 需要能复现的并发/写冲突用例 |
| 子 agent 异步 / worktree | 阶段 2 之后 | 同上，且需要 worktree 隔离的验证用例 |
| RAG / 检索式定位 | 最后 | 需要「找不到代码」的失败用例来量化收益 |

## 5. 观察项：先记录，遇到问题再实施

**已关闭：第 8 项（`estimated_prompt` 高估）**——口径已是 `estimate_tokens(self.messages) +
self._fixed_prompt_tokens()`，与 `ContextManager.maybe_compress` 一致；v1.2 重测后 3.4 里那条过期读数已删除。

下面三条是审计里发现的真实问题，但**现在不动手**，只记录触发条件。理由统一：它们对当前
「单进程、按需调用」的使用形态影响很小，而改动面（`Agent.__init__` 签名、UI 可读性）都不止一处。
等到触发条件出现再排期，避免为假想需求付成本。

| # | 问题 | 现状证据（v1.2 复验） | 什么时候该动手 |
|---|---|---|---|
| 9 | root span 是 `SPAN` 非 `AGENT` | `RunTrace.start_run` 未传 `as_type`；全库 AGENT observation 为 0 | 用 Agent Graph 或多 agent 编排时 |
| 10 | metadata 混入 OTel 内部属性 | 仍在。抽样的 12 条 root span metadata 里都有 `resourceAttributes` 与 `scope` | 人工看 trace 觉得 metadata 面板太吵时 |
| 11 | `retime()` 只对 jsonl 后端生效 | `_BaseSpan.retime` 改的是 corecoder 自己的起止时间，Langfuse 侧时长由 SDK 记 | 用 Langfuse 时间线做延迟分析、要按延迟算 P95 时 |

上面两张表格（3.4 与本节）省不下的细节补在这里：SDK 是支持 `as_type="agent"` 的，
重构后 `as_type` 确实传得下去，只是 `start_run` 那一站没传；Langfuse 侧事后开的 span
实测 latency 0.001s，`duration_ms` 只在 metadata 里。

第 9 项动手时要连带确认：改成 `agent` 类型后，Agent Graph 的节点名取 span 名，
应与子 agent 的命名（`agent.sub`）一起规划，不要分两次改。B5 已在 v1.8 落地（tracer 与会话归并），
**命名仍未做**：父与子的 root span 现在同名 `agent.chat`，只在树形状上可区分。
第 11 项不是 bug：`_BaseSpan.retime` 的 docstring 已经写明「自己计时后端不受影响」，
要改就得往 SDK 的 OTel span 里写时间戳，代价跟收益不匹配，先挂着。

## 6. 明确不做

- **不先建 50 条凭空设计的任务集**：没有可比较版本时，指标是猜的（见 2.3）。
- **不在阶段 1 期间改 `corecoder/`**：会混淆归因（见阶段 1 纪律）。
- **不把 L2 评测跑批放进 `tests/`**：`AGENTS.md` 的「测试全程离线」是硬红线，L2 会真调模型。
  跑批若要做，另开目录与独立入口，不进默认 CI。
- **不追 L3 公开 benchmark**：需要容器环境与预算，与当前「单进程 + 无沙箱」的形态不匹配。
- **不为可观测性接第二套后端**：jsonl 与 langfuse 已经两套，再加只会让「数据在哪」更难回答。
- **现在不做第 5 节那三条**：触发条件未出现，改动面却不小（B1–B8 加 C1–C3 已经占满阶段 0 的预算）。
- **不给 audit 再造一套 id**：join 只靠一个 id 空间。v1.8 落地的方向是「**corecoder 采用后端的
  id**」（`_LangfuseSpan` 读 SDK 公开的 `trace_id` / `id`），而不是为审计行另生一套号。
  本文先前写的方向止于「不让 SDK 接受我们的 id」（C2 原方案），当时把「回写后端 id」夸成了
  要 `getattr` 进 OTel 内部的硬依赖——实测那两个属性是公开的，所以取了便宜的那条。
- **阶段 0 不给 `RunTrace` 加「无 root 就不开 span」的判据**：N3 那 152 条孤儿全部来自测试直调
  内部方法，生产路径没有证据。先靠 0-1 把 trace 关掉让噪音归零；阶段 1 若真出现孤儿，
  再决定是拦下来还是报警——那时才有资格说「加一层防护」。

## 7. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-10-26 | 初稿。确立「清噪音 → dogfood → 固化用例 → 按证据优化」的顺序与验收标准 |
| v1.1 | 2026-10-26 | 补 Langfuse 侧实测与 0A/0B 拆分，细节见下 |
| v1.2 | 2026-10-26 | tracing/audit 重构后全量复验，见下 |
| v1.3 | 2026-10-26 | 阶段 0A 与 B2 落地（只动 `tests/conftest.py` 与 `.env`），附实测验收与第一条真实观察 |
| v1.4 | 2026-10-26 | C1 落地（本轮开始动 `corecoder/agent.py`），按 TDD 先失败后通过；3.1 读数重测 |
| v1.5 | 2026-10-26 | 缺陷 2（`error_type`）与 0-3 / B3（价目表）落地，阶段 0A 四条至此全部完成 |
| v1.6 | 2026-10-26 | `corecoder/` 零改动；按重构后的代码逐条复核待做项，改写 B4、补 B1/B8/3.3 |
| v1.7 | 2026-10-26 | 0D 落地：`CORECODER_AUDIT_KEEP` 使保留上限可配；3.1 与 3.3 重写 |
| v1.8 | 2026-10-26 | B1/B4/B5/B8 与 C2 五条一起落地，0B 只剩 B7；推翻并改写了两条旧判断 |

v1.1：3.4 加了 Langfuse 侧读数；阶段 0 拆成 0A/0B，增 B1–B7 与六条约束；第 5 节只留第 9–10 项
（第 8 项当时判定已修复）；0-1 补上 langfuse 后端的隔离盲点。

v1.2：

- 3.1 与 3.4 全部重测。信噪比从 302:5 恶化到 511:3，trace 从 205 涨到 2703
- B3 改判「代码侧已完成」（`cost_details` 已写，13/1000 非零）、B6 降级、新增 B8；0B 约束六条扩到八条
- 新增 3.5（重构带回的 N1–N4）与阶段 0C（C1–C3）
- 删除第 8 项的过期读数，压成一行「已关闭」；观察项补到三条（新增第 11 项 `retime()`）
- 符号随改名更新：`observability.py` → `audit.py`，`prune_old_logs` 的落点同变
- 更正一条先前的错误结论：`# noqa: BLE001, S110` 不会让 CI 红，`pyproject.toml` 没开 `select`，
  默认规则集不含 RUF100

v1.3：

- 0-1 落地：`tests/conftest.py` 新增 `_observability_isolated`，同时堵住了三个出口
  （audit 文件、`CORECODER_AUDIT_STREAM` 与 `CORECODER_LOG_FILE` 两个新 sink、langfuse 后端）
- 0-2 / 0-4 / B2 落地：`.env` 加三行；输出上限不猜，用一次请求从服务商的 400 里拿到 131072
- N3 的测试侧成因随之堵住（C3）；存量 152 条孤儿不会自己消失，但不再生
- 剩下待做：0-3（价目表）、B1/B4/B5/B7/B8、C1/C2 ——其中 C1 已在 v1.4 落地
- 本轮 `corecoder/` 零改动：三命令结果与改前一致（351 passed / 1 skipped、compileall OK、ruff 绿）

v1.4：

- C1 落地，改动全在 `corecoder/agent.py` 的三处：`_RunStats.outcome` 默认值 `max_rounds` → `error`、
  `chat()` 的 `except (Exception, KeyboardInterrupt)` → `except BaseException`、`_run_rounds` 耗尽分支显式标
  `max_rounds`。循环形状与三条兜底分支未动（AGENTS.md 1.1）
- 新增 `test_run_summary_never_files_a_crash_as_round_exhaustion`：先确认它在未修复代码上失败
  （读出 `max_rounds`），再确认修复后通过；旧用例只测传播不测标签，所以它们全绿也发现不了这个 bug
- 3.1 重测：`run_summary` 行 514 → 577，真实运行 3 → 4 次；同时现场印证了 3.3 的保留上限——
  audit 字节数从 809652 被裁到 790611，而 Langfuse trace 保住在 2812 不动
- 缺陷 2 补一条活证据：577 条 `run_summary` 里带 `error_type` 的是 0 条（只进 root span，不进审计行）
- 剩下待做：0-3（价目表）、B1/B4/B5/B7/B8、C2（0-3 与 B3 已在 v1.5 落地）

v1.5：

- 缺陷 2 落地：`_log_run_summary` 多写一个 `error_type`（空值沿用 `cost_usd` 的 `None` 写法）；
  新增 `test_run_summary_names_the_error_that_ended_the_run`，先以 `KeyError: 'error_type'` 失败、后转绿
- 0-3 / B3 落地：新建 `~/.corecoder/pricing.json`，单价取官方价目页（2026-10-21 更新）的
  `mimo-v2.6-flash` 实时推理非缓存价 $0.14 / $0.28；真实运行实测 `cost_usd=0.001515` 与反算吻合
- 阶段 0A 四条至此全部落地；`run_summary` 的 `outcome` / `error_type` / `cost_usd` / `build` 全部可用
- 已知口径缺陷（没修，只是记下来）：`estimated_cost` 不区分缓存命中 token，所以 `cost_usd` 是上界
- 剩下待做：B1/B4/B5/B7/B8、C2；三命令 353 passed / 1 skipped、compileall OK、ruff 绿
- 新增的 `pricing.json` 是真实文件，但它在测试里同样被隔离（conftest 已把 `llm.PRICING_FILE` 改指
  `tmp_path`）：带着它跑完整一遍 `pytest`，audit 575763 → 575763 字节、trace 2813 → 2813 条，零增长

v1.6：

- 起因是一句我说错的话：我拿重构前的记忆去断言「第 11 项描述的机制已不存在」，而第 11 项写的是
  `retime()`。重读全文后逐条对照代码复核，结论相反：`retime`、`_origin` 的自造 id、`start_run` 不传
  `as_type`/`tags`/`user_id` 均仍在，所以 N2/C2、N4、B5、B7、B8 与第 9、11 项**原文保留**
- 只有一处真的被重构改写了形态：B4。改写后口径是「正文按轮记在 `llm.round`（`_model_round` 里
  无条件调 `capture_output`），缺的是 root/trace 层与三个兜底返回值」，不再是「trace 里看不到答案」
- 顺带一个降本发现：`_finish_run` 现在已经在传 `fields["error"]`，`tool_failed` 也在传，所以 B1 从
  「数据流改造」降为 `_LangfuseSpan._close` 里加两行
- 3.3 补全了「显式路径不被剪」的机制细节（`if explicit is None:`）与用它归档的两笔代价；
  阶段 1 开工前要先定归档策略，否则一两周后的第一手失败现场会被 `keep=10` 淘掉
- 本轮验证：`pytest tests/ -q` 353 passed / 1 skipped（与 v1.5 一致，说明 C1 改动与上游重构共存无冲突）；
  新增只读探针 `.tmp/b4_probe.py`（挨一条 dogfood trace 打印 output / tags / userId / scores）

v1.7：

- 0D 落地，改动三处：`corecoder/audit.py`（新增 `_DEFAULT_KEEP`，`prune_old_logs` 默认参数与
  `_open_audit_file` 改走 `_env_int("CORECODER_AUDIT_KEEP", …)`）、`tests/test_audit.py`（新增一条
  先失败后通过的用例）、`tests/conftest.py`（隔离这个新变量）；`.env` 加 `CORECODER_AUDIT_KEEP=500`
- 推翻了自己的一个建议：v1.6 之前我一直把「阶段 1 不许改 `corecoder/`」当成不能改代码的理由，
  而阶段 1 还没开始——阶段 0 的存在意义就是改代码。因此废弃了「每天手动搬文件」那条路线
- 量出一个比预估严重的事实：保留窗口数的不是运行次数而是**进程数**，10 个 audit 文件时间戳
  全是当天的——不改代码的话，dogfood 第一周的失败现场到第二周就查无实据
- 记账一条坑：`--demo` 分支在 `Config.from_env()` 之前就转 `run_demo()`，所以它不读 `.env`；
  这个顺序不该修，修了 demo 会继承 `CORECODER_TRACE=langfuse` 并往真实 Langfuse 写
- 阶段 0 至此只剩 0B（B1/B4/B5/B7/B8）与 0C 的 C2；三命令 354 passed / 1 skipped、compileall OK、ruff 绿

v1.8：

- 五条一起落地，改动四处：`corecoder/tracing.py`（`_LangfuseSpan.__init__` 采用后端 id、`_close` 写
  `level`/`status_message`、`RunTrace.start_run` 多两个透传形参）、`corecoder/agent.py`
  （`_RunStats.answer`、`Agent._labels`、`_finish_run` 写 root `output`）、`corecoder/tools/agent.py`
  （子 agent 继承 `tracer` 与 `session_id`）、`tests/`（conftest 隔离两个新变量 + 十条新用例）。
  循环形状与三条兜底分支未动（AGENTS.md 1.1），工具集计数未动（1.2）
- **推翻两条自己先前的判断**，都写清了证据：C2 的「回写后端 id 要 `getattr` 进 OTel 内部，所以不做」
  是错的（那两个属性公开）；B5 的「子 agent 断链」症状记反了（它有 trace 且能嵌套，真问题是无视
  注入的 tracer）。另修正一条写错的口径：`chat()` 只有两个正常返回，`[interrupted]` 不是返回值
- **B8 按附录 B 的警告选了调用方取值**（`Agent._labels`）而不是在 `tracing.py` 里 `os.getenv`，
  标签与 audit 共用一个模块；tag 带 `build:` 前缀；新增 `CORECODER_USER`（不设不记）
- 新增一条限制（3.3 第 4 条）：子 agent 与父同会话后，`run_summary` 每回合可能两行，且子那行的
  成本增量是父的子集——按会话求和成本会高估，改用 `llm_call` 行
- B7 故意推后：SDK 侧 API 实测全部公开可用，缺的是判断来源；凭 `outcome` 打分只会造出自家评分
- 本轮验证：三命令 **364 passed / 1 skipped**（354 + 十条新用例）、compileall 无输出、ruff 绿；
  每条新用例都先跑成失败。另跑一次真实后端的在线复核（`ScriptedLLM`，不调模型）：
  审计行的 `trace_id` 命中 REST 返回 200，新 trace 实测 `tags=['build:probe']`、`userId=synapse`、
  `trace.output='parent answer'`，失败运行的工具 span 与 root 都是 `level=ERROR`，
  子 agent 挂在父的 `tool.agent` 下且两条运行同一 `sessionId`（探针 `.tmp/live_check.py`，不入库）

## 附录 A：读数命令

全部在仓库根目录执行（Windows cmd），输出即本文 3.1 的统计口径。命令均为单行，不要按显示宽度断行。

**A.1 按模型统计运行次数（信噪比）**

```bat
.venv\Scripts\python.exe -c "import glob,os,re,collections;P=os.path.join(os.path.expanduser('~'),'.corecoder','audit.*.log');ls=[l for f in glob.glob(P) for l in open(f,encoding='utf-8',errors='replace') if l.startswith('event=run_summary')];print('rows',len(ls));print(collections.Counter([re.search(r'model=([^ ]+)',l).group(1) for l in ls if 'model=' in l]).most_common())"
```

**A.2 按模型 × 结果统计（成败分布）**

```bat
.venv\Scripts\python.exe -c "import glob,os,re,collections;P=os.path.join(os.path.expanduser('~'),'.corecoder','audit.*.log');ls=[l for f in glob.glob(P) for l in open(f,encoding='utf-8',errors='replace') if l.startswith('event=run_summary')];print(collections.Counter([(re.search(r'model=([^ ]+)',l).group(1),re.search(r'outcome=([^ ]+)',l).group(1)) for l in ls if 'model=' in l and 'outcome=' in l]).most_common())"
```

**A.3 事件类型分布（健康度面板）**

```bat
.venv\Scripts\python.exe -c "import glob,os,collections;P=os.path.join(os.path.expanduser('~'),'.corecoder','audit.*.log');C=collections.Counter();[C.update([l.split()[0].split('=')[1]]) for f in glob.glob(P) for l in open(f,encoding='utf-8',errors='replace') if l.startswith('event=')];print(C.most_common(15))"
```

关注这几项：`backfill`（有 tool 调用没拿到回复）、`output_truncated`（输出上限不够）、
`context_overflow` / `context_forced`（上下文被拒后自救）、`provider_fallback`（provider 不吃某个参数）。

**A.4 trace 侧按模型统计**（只对 jsonl 后端有意义）

```bat
.venv\Scripts\python.exe -c "import glob,json,os,collections;P=os.path.join(os.path.expanduser('~'),'.corecoder','traces.*.jsonl');ds=[json.loads(l) for f in glob.glob(P) for l in open(f,encoding='utf-8',errors='replace') if l.strip().startswith('{') and l.strip().endswith('}')];print('spans',len(ds));print(collections.Counter([d.get('attrs',{}).get('model') for d in ds]).most_common(8))"
```

v1.2 实测：`.env` 里 `CORECODER_TRACE=langfuse` 后这个 glob 只命中 10-25 之前的旧文件，
**jsonl 分支不再新增**。所以它现在是「核对历史存档」的工具，不是当前读数；
当前 trace 读数走 A.6 / A.7。想要能落文件的 trace 读数，把 `CORECODER_TRACE` 改成 `jsonl`。

**A.5 只统计打了标的真实运行（阶段 0 完成后才有数据）**

```bat
.venv\Scripts\python.exe -c "import glob,os,re,collections;P=os.path.join(os.path.expanduser('~'),'.corecoder','audit.*.log');ls=[l for f in glob.glob(P) for l in open(f,encoding='utf-8',errors='replace') if l.startswith('event=run_summary') and 'build=dogfood' in l];print(len(ls));print(collections.Counter([re.search(r'outcome=([^ ]+)',l).group(1) for l in ls if 'outcome=' in l]).most_common())"
```

**A.6 Langfuse 侧读数（UI 三步验收）**

阶段 0B 完成后，用浏览器打开本地 Langfuse，按这三步确认「筛得动」：

1. 顶部环境选择器选 `dogfood`——列表里应只剩真实运行，`tool.boom` 这类测试桩消失
2. 过滤 `Level = ERROR`——应能筛到失败调用（若库已轮转清空，用一次故意失败的调用复现）
   （**v1.8 已可用**：CoreCoder 自己开始写 `level=ERROR` 了）
3. 打开任一 `agent.chat`——trace 详情应同时有 input 与 output，Scores 区应有一条 `outcome` 分类分
   （v1.6 提醒：input 现在已经有了，是 `capture_request` 写的；真正缺的只有 root 的 output。
   别拿「能看到正文」当 B4 已修——正文在 `llm.round` 那一层，本来就在。
   **v1.8：root 的 output 已落地**，列表页那一栏不再永远为空；只有 Scores 那一步仍无数据（B7））

本文 3.4 的数字来自一次只读探针（REST `/api/public/traces` 与 `/api/public/observations`），
脚本放在 `.tmp/` 下、不入库，所以不在这里给命令；复算请用上面的 UI 三步，或直接调 A.7 的端点。

**A.7 Langfuse REST 读数（3.4 与 C2 验收的复算口径）**

直接在 cmd 里跑，`<pk:sk>` 换成 `.env` 里的 `LANGFUSE_PUBLIC_KEY:LANGFUSE_SECRET_KEY`。
只读，不写任何数据。

```bat
curl -s -u <pk:sk> "http://localhost:3000/api/public/traces?limit=1" | .venv\Scripts\python.exe -c "import json,sys;print('traces total', json.load(sys.stdin)['meta']['totalItems'])"
```

```bat
curl -s -u <pk:sk> "http://localhost:3000/api/public/scores?limit=1" | .venv\Scripts\python.exe -c "import json,sys;print('scores total', json.load(sys.stdin)['meta']['totalItems'])"
```

第一条量的是 3.4 的「trace 总量」，第二条量的是 B7（现在应为 0）。

C2 的验收也是一条命令：把审计行里的 `trace_id` 填进去，返回 **200** 而不再是个 **404**：

```bat
curl -s -o NUL -w "%{http_code}\n" -u <pk:sk> "http://localhost:3000/api/public/traces/<审计行里的 trace_id>"
```

v1.2 的实测基线：审计行共 531 个 `trace_id`，与 Langfuse 最新 100 条 trace id **交集 0**，
按这条路子查一律 404（见 3.5 的 N2）。**v1.8 已反转为 200**（C2），且新写入的 id 形状本身就是信号：
`span_id` 从自造的 32 位 hex 变成后端的 16 位 hex（旧审计行仍是 32 位，那是采用前的数据）。
同理可验 observation 类型分布：`/api/public/observations?type=EVENT&limit=1` 的 `meta.totalItems` 就是
3.4 表里的 EVENT 计数。另两条 v1.8 新增的复算口径（均只读）：

```bat
curl -s -u <pk:sk> "http://localhost:3000/api/public/traces?limit=1&tags=build:dogfood" | .venv\Scripts\python.exe -c "import json,sys;d=json.load(sys.stdin);print('tagged',d['meta']['totalItems'],[(t.get('sessionId'),t.get('userId'),str(t.get('output'))[:40]) for t in d['data'][:3]])"
```

量的是 B8（tags 有值）与 B4（trace 层 output 有值）；`userId` 只在 `.env` 设了 `CORECODER_USER` 时非空。

```bat
curl -s -u <pk:sk> "http://localhost:3000/api/public/observations?level=ERROR&limit=3" | .venv\Scripts\python.exe -c "import json,sys;d=json.load(sys.stdin);print('ERROR total',d['meta']['totalItems']);print([(o.get('name'),str(o.get('statusMessage'))[:40]) for o in d['data']])"
```

量的是 B1：v1.2 时全库只有 2 条且都是探针自写的，v1.8 起 CoreCoder 自己会往这里写。

**A.8 保留上限是否生效（0D 的复算口径）**

先看一眼当前值。注意它必须先加载 `.env`，否则只会读到 shell 环境（没设过就是内置 10）：

```bat
.venv\Scripts\python.exe -c "import glob,os;from corecoder import Config,audit;Config.from_env();print('files',len(glob.glob(os.path.expanduser('~/.corecoder/audit.*.log'))),'keep',audit._env_int('CORECODER_AUDIT_KEEP',audit._DEFAULT_KEEP))"
```

要看“真的不再剪了”，跑一次与真实运行同一顺序的动作（先 `from_env()` 再触发首次写入）：

```bat
.venv\Scripts\python.exe -c "import glob,os;from corecoder import Config,audit;P=os.path.expanduser('~/.corecoder/audit.*.log');b=len(glob.glob(P));Config.from_env();audit.log_event('keep_probe');print('before',b,'after',len(glob.glob(P)))"
```

上限已生效时输出是 `before 10 after 11`；未生效时是 `before 10 after 10`（新建一个、剪掉一个）。
这条命令会往 audit 多写一行 `keep_probe`，它是真实进程产出的数据，不影响 3.1 的 `run_summary` 口径。
**不要用 `corecoder --demo` 做这个验收**——demo 不加载 `.env`，永远看到旧上限（见 0D 的坑）。

## 附录 B：`CORECODER_BUILD` 字段说明

**它是什么**：同一个变量现在进两个地方——运行结束那行 `run_summary` 的 `build` 字段，以及 trace 层的
`tags`（v1.8 随 B8）。v1.7 之前它只进 audit。

- 两个读取点，都在 `corecoder/agent.py`：`Agent._log_run_summary`（写 audit 的 `build=`）与
  `Agent._labels`（写 trace 的 `tags=["build:…"]`）。**故意不放在 `tracing.py`**，理由见本文末尾
  「与 B8 的关系」
- 引用它的测试（全仓库只这两处）：`tests/test_audit.py` 的
  `test_run_summary_carries_version_and_build`（断言值为 `abc123`）与 `tests/test_tracing.py` 的
  `test_the_run_is_tagged_and_attributed_at_trace_level`
- **不改变 agent 任何行为**：不是开关，不是功能门
- 两份 README、`AGENTS.md`、`article/` 均未提及它

**原意**：构建标识（与 `version` 并列，测试里用 git sha 风格的 `abc123`），用来回答「这个 bug 是哪个构建引入的」。

**本计划怎么用它**：复用它当运行来源标签（`dogfood`）——语义上都是给运行分组的维度，但这是**复用而非原设计用途**。
如果将来觉得别扭，正规做法是新增 `CORECODER_RUN_TAG`，代价是改 `Agent._log_run_summary`、补一条测试、同步 `AGENTS.md` 的配置说明。

**一个便利细节**：`Config._load_dotenv` 用 `load_dotenv(..., override=False)`，即**进程环境变量优先于 `.env`**。
所以把 `CORECODER_BUILD=dogfood` 写进 `.env` 当默认值，临时要换标签时 `set CORECODER_BUILD=xxx` 即可覆盖，不用改文件。

**与 B8 的关系**（v1.8 已落地）：本变量现在**audit 与 trace 两边都有**——`Agent._log_run_summary` 写
`build=dogfood`，`Agent._labels` 写 `tags=["build:dogfood"]`。落地方式就是本节原先建议的那条：
由 `chat()` 把它当参数传进 `RunTrace.start_run`，而**不在 `tracing.py` 里 `os.getenv`**。
原警告仍然成立且被本轮验证：拆两个模块各读一半，将来改标签只改中一个就会两边不一致；
现在两个读取点同在一个文件，`tracing.py` 只负责传递。与之同机制的新变量是 `CORECODER_USER`
（只读开关，不设则 `userId=None`），也放在 `Agent._labels` 里读。
