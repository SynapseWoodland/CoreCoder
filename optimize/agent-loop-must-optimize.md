# Agent Loop 必须优化项（生产化审查）

- 审查对象：`corecoder/agent.py` 中 `Agent.chat()` 主循环（v1.0 写作时为第 70–124 行，v1.2 实测为第 77–138 行）及其直接依赖
- 审查基线：当前工作区 `main` 分支；文中行号均为写作时实测值
- 文档版本：v1.5（2026-10-24）。v1.0 为审查定稿；v1.1 为 M2 实施后的复核回写，新增 0.5 节与 4.f 节，并同步第 7、9、10 节；v1.2 为 M1 实施后的回写，把第 3、7、8 节的状态改为已实施，并同步实测测试数；v1.3 为 M3 实施后的回写，更新第 2、5、7、8 节与 4.f.2 的组表，并同步 10.2、10.5 的实测值；v1.4 为 M4 实施后的回写，更新第 2、6、7、8 节与 4.f.2 的组表，并同步 10.2、10.5 的实测值；v1.5 为第 9 节残留项落地的回写：可观测性、Ctrl+C 取消与 R3 已实施，R8 的示例修复已核实，R6/R7 仍按原决定不做
- 读者：未参与前序讨论、需要直接实施改造的工程师
- 路径约定：除特别说明外，所有路径相对仓库根目录 `D:\github\CoreCoder`

> 本文只覆盖最近一次复核后判定为“必须优化”的 4 项，不包含“建议优化”和“可暂不处理”的内容。第 9 节单独说明边界与残留风险。
>
> v1.1 增补：M2 已按第 4 节实施。实施后追加了两轮生产级复核，判定出两项必须修的次生问题，均已并入代码并回写在 4.f；未实施的建议项与残留风险，按原有格式并入第 9 节。
>
> v1.2 增补：M1 已按第 3 节实施，采用 `try/finally` 无条件回填（幂等）。实施记录、组 10 的 5 条用例与实测结果回写在 3.c–3.e；第 7、8 节标为已实施，4.f.2 的测试数与组数同步为实测值。
>
> v1.3 增补：M3 已按第 5 节实施（请求级兜底 + 固定开销计入估算）。实施记录、组 11 的 6 条用例与实测结果回写在 5.c–5.e；第 2、7、8 节标为已实施，4.f.2 的组表、10.2 的 grep 预期与 10.5 的行数同步为实测值。
>
> v1.4 增补：M4 已按第 6 节实施（工具结果出口统一封顶与脱敏 + `read_file` 文件大小上限）。实施记录、组 12 的 6 条用例与实测结果回写在 6.c–6.e；第 2、7、8 节标为已实施，4.f.2 的组表、10.2 的 grep 预期与 10.5 的行数同步为实测值。至此 4 项必须优化全部实施完成；第 9 节的残留项保持原样。
>
> v1.5 增补：第 9 节的残留项按评审后的取舍落地——可观测性（审计事件、成本与 `run_summary`、trace 接口 + JSONL/Langfuse 适配器）、并行工具的 Ctrl+C 取消、R3 坏参数 head+tail 诊断均已实施；R8（`examples/m2` 判定失效）已在 `82f2c7f` 修复，本次只做核实；R6/R7 按原决定不做。新增用例分布见各提交与 `tests/test_interrupt_cancel.py`、`tests/test_bad_argument_diagnostics.py`；全量与真实 provider 脚本实测值见 10.5。

## 0. 与上一轮审查的对应关系

### 0.1 条目数量与命名

条目数量仍为 4 项，编号在本轮固定为 M1–M4，便于追踪矩阵和实施顺序引用：

| 本轮编号 | 上一轮编号 | 名称 |
|---|---|---|
| M1 | 必须项 1 | 异常回填覆盖面不足 |
| M2 | 必须项 2 | 截断响应未识别 |
| M3 | 必须项 3 | 上下文超限无兜底 |
| M4 | 必须项 4 | 工具结果无统一治理 |

### 0.2 范围与措辞调整（及原因）

| 条目 | 上一轮表述 | 本轮定稿 | 调整原因 |
|---|---|---|---|
| M1 | “任何非 Ctrl+C 异常都会留下悬空 tool_calls” | “存在无法兜住的异常逃逸点，默认 CLI 路径大多安全” | 默认 CLI 传入的 `on_tool`、`ask` 回调自身安全；真正风险在库嵌入、自定义回调与 `BaseException` |
| M2 | “半截 JSON 会被误执行” | “内置工具会被参数校验拦下；只有 MCP 工具会真正空参数执行” | `_exec_tool` 的 `bind` 校验对内置工具生效，而 `MCPTool.execute(**kwargs)` 永远绑定成功 |
| M3 | “估算可能偏低” | “结构性漏算 system prompt 与 tool schema，90% 阈值形同虚设” | `maybe_compress` 只接收 `self.messages`，未计入请求固定开销 |
| M4 | “没有统一截断与脱敏” | “截断口径不一，且 read_file 与 MCP 两处漏网；全仓无脱敏” | bash/fetch/edit/grep/子 agent 已有各自上限，问题在入口不统一与两处缺口 |

### 0.3 审查修订（不影响必须项清单）

上一轮在分析并行执行时提到“Ctrl+C 后 worker 变成孤儿线程”，该说法不准确，现更正：

- 实际行为：主线程在 `future.result()`（`corecoder/agent.py:209`）收到 `KeyboardInterrupt` 后，`with ThreadPoolExecutor(...)` 的 `__exit__` 会执行 `shutdown(wait=True)`；Python 3.9+ 默认 `cancel_futures=False`，因此主线程会阻塞等待整批任务（包括尚未开始的任务）结束，然后才执行回填并重抛。
- 用户感受：Ctrl+C 看起来“卡住”，直到批次跑完（最长可到 bash 的 120 秒超时，`corecoder/tools/bash.py:77`）。
- 影响：这属于中断体验问题，不在本次 4 项必须清单内，已在第 9 节列为暂不处理并说明残留风险。

### 0.4 不虚构声明

- 文中“现有行为”全部来自对当前工作区源码的阅读，行号可复核。
- 文中“修复后行为”均以“应”表述；任何测试结果在实施前都未运行，不声称已通过。
- 无法从源码确定的事项集中在第 10 节，标注“待核实”并给出核实命令。
- v1.1 例外：4.f 节描述的是已实施并已跑过全量测试的行为，其中的行号与测试数为 v1.1 当时实测值。
- v1.2 例外：第 3 节（M1）同样已实施并跑过全量测试，3.c–3.e 中的代码、行号与命令结果为 v1.2 实测值，不再用“应”表述；第 5、6 节（M3、M4）仍是 v1.0 的“应”表述，实施时请按 10.5 重新定位行号。
- v1.3 例外：第 5 节（M3）已实施并跑过全量测试与真实 provider 脚本，5.c–5.e 为 v1.3 实测值；5.a 保留 v1.0 的问题存档，其中行号是修复前位置。第 6 节（M4）仍是 v1.0 的“应”表述，实施时请按 10.5 重新定位行号。
- v1.4 例外：第 6 节（M4）已实施并跑过全量测试与真实 provider 脚本，6.c–6.e 为 v1.4 实测值；6.a 保留 v1.0 的问题存档，其中行号是修复前位置。第 5 节与第 6 节之外的实施前存档（3.a、4.a–4.e）仍保留原值。

### 0.5 v1.1 复核严重度分级与本文编号的关系

M2 实施完成后追加两轮生产级复核。复核按严重度分级给出条目，**这里的 P1/P2/P3 指复核的严重度，与第 2 节表里的“优先级”列不是同一套坐标**，不要混用：

| 复核严重度 | 含义 | 本文去向 |
|---|---|---|
| P1 | 必须修，否则本次修复不成立 | 已实施，回写 4.f（R1、R2） |
| P2 | 应修，但不阻塞当前修复 | 已修的并入 4.f（R4、R5）；未修的并入第 9 节（R3） |
| P3 | 建议，仅影响噪声与可维护性 | 未实施，并入第 9 节（R6、R7、R8） |

为避免与 M 编号撞车，本轮条目在本文中统一改用 R 前缀：

| 本文编号 | 复核严重度 | 名称 | 状态 |
|---|---|---|---|
| R1 | P1 | 非 length 的坏参数仍被空参数执行 | 已实施，见 4.f.1 |
| R2 | P1 | 截断终止信号只经返回值传出 | 已实施，见 4.f.2 |
| R3 | P2 | `raw_arguments` 无下游消费者，注释与实现不一致 | 未实施，见第 9 节 |
| R4 | P2 | 并行批次的坏参数防线零测试覆盖 | 已实施，见 4.f.2 |
| R5 | P2 | 小 `max_rounds` 下截断仍静默退出 | 已实施，见 4.f.2 |
| R6 | P3 | `on_tool` 早于坏参数判定触发 | 未实施，见第 9 节（并入“并行 `on_tool` 语义”） |
| R7 | P3 | pre-hook 看不到被拒调用，形成审计盲点 | 未实施，见第 9 节 |
| R8 | P3 | `examples/m2_truncated_tool_call.py` 判定逻辑失效 | 未实施，见第 9 节 |

## 1. 审查范围与结论摘要

### 1.1 范围

纳入：

- `Agent.chat()` 主循环、单工具路径、并行路径、`_answer_pending_tool_calls`
- `LLM.chat()` / `_drain()` 的流式解析与错误处理
- `ContextManager.maybe_compress()` 的调用时机与估算
- hooks、permission 的调用点
- `read_file`、MCP 工具结果的入口治理
- 调用方（`cli.py`）的异常兜底

不纳入：

- provider SDK 内部实现、网络栈
- CLI 交互体验与 slash 命令
- session 持久化格式、checkpoints
- “建议优化”和“可暂不处理”项（见第 9 节）

### 1.2 结论摘要

这个 Agent Loop 的“快乐路径”是成熟的：工具调用、并行执行、Ctrl+C 回填、hooks 先于权限、plan mode 门控都有代码和离线测试支撑。生产化的主要差距集中在三个系统性位置：

1. 失败点会撕裂历史：只有 `KeyboardInterrupt` 被回填，其它异常逃逸后留下悬空 `tool_calls`（M1）。
2. 模型输出被截断时静默降级：不读 `finish_reason`，半截参数被当成空参数（M2）。
3. 长会话缺少入口治理与最后防线：估算漏算固定开销，超限后不会自动压缩重试；工具输出没有统一封顶和脱敏（M3、M4）。

## 2. 必须优化项总表

| 编号 | 问题名称 | 用户可见影响 | 严重度 | 优先级 | 涉及文件与行号 |
|---|---|---|---|---|---|
| M1（v1.2 已实施） | 异常回填覆盖面不足 | 一次非 Ctrl+C 失败后，后续每轮请求都可能被 API 拒绝，只能 `/reset` | 高 | P0 | 实施前：`agent.py:90-119,217-231`；现状：`agent.py:113-124,299-313`；`hooks.py:70-88`；`permissions.py:53`；`cli.py:320-332` |
| M2（v1.1 已实施） | 截断响应未识别 | 截断时工具被空参数调用（MCP 可能写坏数据），或模型反复收到“参数错误”而重试 | 高 | P0 | 实施前：`llm.py:209-277`（解析在 `263-266`）、`agent.py:153-168`、`mcp.py:206-207`；现状：`llm.py:38,235,250-252,319`；`agent.py:26,114-119,151-161` |
| M3（v1.3 已实施） | 上下文超限无兜底 | 长会话突然报 `maximum context length`，本轮失败且不会自动恢复 | 高 | P1 | 实施前：`agent.py:73,76,122`、`context.py:28-45,52-54,142-157`、`llm.py:180-190`；现状：`llm.py:108-120`；`context.py:56-86,152`；`agent.py:78-82,84-107,149` |
| M4（v1.4 已实施） | 工具结果无统一治理 | 一次大输出挤爆上下文；密钥回显进入历史并可能被 `/save` 落盘 | 中高 | P1 | 实施前：`agent.py:153-168`、`read.py:34-53`；现状：`agent.py:43,49,253-269,371-383`；`read.py:8,41-48` |

> 严重度衡量“用户是否会拿到错误结果或丢失数据”；优先级衡量“本轮实施顺序”。M1/M2 为 P0，因为它们在默认参数下就可能发生，且后果是会话不可用或副作用错误。

## 3. M1 异常回填覆盖面不足

> **v1.2 状态：已实施。** 落点 `corecoder/agent.py:115-124`，做法是 `try/finally` 无条件回填，详见 3.c；用例组 10（5 条）见 3.d；实测结果见 3.e。3.a–3.b 保留的是实施前的现状与方案对比，作为设计依据。

### 3.a 问题现状

**先讲后果。** 模型说“我要调用工具”，agent 把这句意图记进对话，然后去执行。如果执行途中发生一个不是 Ctrl+C 的错误（界面回调崩了、权限回调崩了、工具直接退出进程），agent 会把异常抛给上层，但对话里已经留下“我要调用工具”的记录，却没有对应的“工具结果”。下一次请求时，API 看到这种不成对的历史会拒绝整个请求。用户感受是：聊着聊着，之后每句话都报错，只能 `/reset` 重开。

**术语。** 这叫悬空 `tool_calls`（dangling tool_calls）：一条 `assistant` 消息包含 `tool_calls`，但缺少与每个 `id` 一一对应的 `role="tool"` 消息。OpenAI 兼容接口要求两者必须配对。

**触发条件（按真实可达性排序）。**

| 序号 | 逃逸点 | 代码位置 | 是否真实可达 |
|---|---|---|---|
| 1 | 调用方传入的 `on_tool` 回调抛异常 | `agent.py:95-96`、`179-181` | 可达。CLI 传的是 `console.print`（`cli.py:311-312`），安全；库嵌入方不受控 |
| 2 | `Permission.ask` 自定义回调抛异常 | `permissions.py:53` | 可达。CLI 的 `_ask_permission` 捕获 `EOFError/KeyboardInterrupt`（`cli.py:126-130`），其它异常会冒 |
| 3 | hooks 的 `_fire` 只捕获 `TimeoutExpired/OSError` | `hooks.py:81` | 边缘可达。`run_shell` 抛其它异常，或 payload 无法 JSON 序列化时会冒 |
| 4 | 工具抛 `BaseException` 子类（如 `SystemExit`） | `agent.py:167` 只捕获 `Exception` | 可达。自定义工具、MCP 适配层都可能 |
| 5 | 并行 worker 里的异常经 `future.result()` 重抛 | `agent.py:209` | 可达（同上，仅 `BaseException` 能穿过 `_exec_tool`） |
| 6 | `_post_hooks` 抛异常 | `agent.py:100`、`213-214` | 边缘可达（同第 3 条） |

**真实执行路径（逐行走）。**

- 单工具：`72` 写入 user → `76` 调 LLM → `84` 判断有 `tool_calls` → `90` 追加 assistant 消息 → `95-96` 触发 `on_tool` → `97` 先 hooks 后权限 → `99` 执行 → `100` post-hook → `101-105` 追加 tool 回复。
- 并行：`90` 追加 assistant → `108` 进入 `_exec_tools_parallel` → `179-181` 对所有调用触发 `on_tool` → `185` 逐个 hooks/权限 → `190` 线程池 → `209` 收集结果 → `213-214` post-hook → 返回后 `109-114` 追加 tool 回复。
- 兜底只存在于 `115-119`：`except KeyboardInterrupt` 时回填并重抛。异常类型不是 `KeyboardInterrupt` 时，这段代码不执行，`101-105` / `109-114` 也不执行。

**失败表现。**

- 下一轮 `_full_messages()`（`agent.py:53-65`）把脏历史原样发出，API 返回 400（`tool_calls` 缺少对应 `tool` 消息）。
- REPL 的泛 `except Exception`（`cli.py:330-332`）只打印 `Error: ...` 并继续，脏历史保留，于是每轮都失败。
- one-shot 模式打印错误并以退出码 1 结束（`cli.py:159-162`）。

**现有兜底。**

- `_answer_pending_tool_calls`（v1.0 行号 `agent.py:217-231`，现为 `299-313`）本身是幂等的：先收集已应答的 `tool_call_id`，只补缺失的，内容为 `[interrupted]`。已有测试覆盖其幂等性（`tests/test_core.py:460-476`）和 Ctrl+C 场景（`tests/test_safety_matrix.py:171-208`）。
- 问题不在这个函数的能力，而在挂载点：它只挂在 `KeyboardInterrupt` 上（v1.2 已改为挂在 `finally`）。

**测试缺口（v1.0 现状）。** 当时没有覆盖：`on_tool` 抛异常、`ask` 抛异常、工具抛 `SystemExit`、并行 worker 逃逸、post-hook 抛异常。**v1.2**：前四项已由组 10 补上（见 3.d）；post-hook 抛异常走同一个 `finally`，机制上已被覆盖，但没有独立用例（第 9 节仍列为残留）。核实命令见第 10.2 节。

### 3.b 方案决策

**候选方案。**

- A：把 `except KeyboardInterrupt` 改为 `except BaseException`，回填后重抛。
- B：改为 `try/finally`，在 `finally` 中回填，异常自然传播。
- C：在 `chat()` 入口扫描历史并修复悬空 `tool_calls`（自愈），执行中不回填。
- D：把工具执行重构为返回结构化结果的 `_run_round()`，主循环不向外抛异常。

**对比。**

| 维度 | A 捕获 BaseException | B try/finally | C 入口自愈 | D 结构化重构 |
|---|---|---|---|---|
| 正确性 | 高，覆盖异常路径 | 最高，覆盖 return/raise 所有退出 | 中，只能修历史，不能阻止本轮写入 | 高 |
| 改动范围 | 1 行 | 约 4 行 | 中，新增入口扫描 | 大，两条执行路径都要改 |
| 实现复杂度 | 低 | 低 | 中 | 高 |
| 兼容性 | 完全兼容 | 完全兼容 | 完全兼容 | 内部接口变化 |
| 性能与成本 | 无额外开销 | 每轮一次 O(历史) 扫描；成功路径是 no-op | 每次 chat 入口一次扫描 | 重构与回归成本最高 |
| 风险 | 未来若有人在 try 内提前 return，会绕过回填 | 极低 | 掩盖根因，且修复晚于错误发生 | 超出本轮范围，易引入新问题 |

**推荐：B（try/finally）。**

- 决策依据：`finally` 是语言级不变量，不依赖异常类型枚举；无论未来新增什么异常或提前 return，都能保证“本轮每个 `tool_call_id` 都有回复”。
- 为什么不用 A：成本几乎相同，但只覆盖异常路径；B 更彻底。
- 为什么不用 C：它是事后修补，错误已经发生；且入口扫描会掩盖“执行中未回填”的根因。
- 为什么不用 D：属于架构级重构，应作为后续独立议题，不应混进本轮“止血”改动。

**v1.2 决策已落地：** 采用 B，未采用 A/C/D。实际改动只做了两件事：删掉 `except KeyboardInterrupt` 分支、把 `except` 换成 `finally`；`_answer_pending_tool_calls` 的实现一字未改，回填内容仍是既有的 `[interrupted]`。

### 3.c 具体实施

**最小可行改动（M1-min）——已实施。**

文件：`corecoder/agent.py`。实测落地位置 `113-124`，结构如下（与仓库代码逐字一致）：

```python
            self.messages.append(resp.message)

            try:
                self._execute_round(resp, on_tool)
            finally:
                # any escape from the round (Ctrl+C, a crashing on_tool or ask
                # callback, a BaseException from a tool) would leave the
                # assistant tool_calls message without replies and poison
                # every later request, so the backfill is unconditional and
                # idempotent: on a clean round it is a no-op
                self._answer_pending_tool_calls(resp.tool_calls)
```

三个关键性质（都是选 B 的直接理由，也是回归用例要钉住的东西）：

- **无条件**：`finally` 不判断异常类型，也不用枚举 `KeyboardInterrupt` / `Exception` / `BaseException`。只要本轮已把 assistant 消息写进历史，退出时就会执行回填。
- **幂等**：`_answer_pending_tool_calls`（`agent.py:299-313`）先收集所有已应答的 `tool_call_id`，只补缺失的那些，内容为 `[interrupted]`。因此成功轮次是 no-op，不会重复写回复；Ctrl+C 场景也与旧行为完全一致。
- **覆盖三类真实逃逸点**：
  1. `on_tool` 回调崩溃 —— 单条路径在 `_execute_round` 入口触发，并行路径在 `_exec_tools_parallel` 里先对所有调用统一触发，两处都在 `try` 内。
  2. `ask` 权限回调崩溃 —— 单条路径的 `_permit`、并行路径的短路列表推导都在 `try` 内；崩溃前工具未执行，因此只补历史、不产生副作用。
  3. 工具抛 `BaseException`（如 `SystemExit`） —— `_exec_tool` 的 `except Exception`（`agent.py:246`）漏掉它；并行路径还会经 `future.result()` 在主线程重抛。两种情况都从 `try` 逃逸，`finally` 补齐全部未应答的 id（包括已执行完的兄弟调用）。

要点：

- 删除 `except KeyboardInterrupt` 分支；`finally` 已覆盖该场景，且不会吞掉异常。
- 不改 `_answer_pending_tool_calls` 的实现。
- `try` 的起点保持在 `self.messages.append(resp.message)` 之后，确保只有“已写入 assistant 消息”的轮次才需要回填。

**控制流变化（下表“改后”列即 v1.2 已实现的行为）。**

| 场景 | 现在 | 改后 |
|---|---|---|
| 正常完成 | 追加 tool 回复，继续下一轮 | 追加 tool 回复；finally 扫描发现无缺失，no-op |
| Ctrl+C | 回填并重抛 | finally 回填，异常继续重抛 |
| 其它异常 | 不回填，异常逃逸 | finally 回填，异常继续重抛 |
| 未来新增提前 return | 不回填 | finally 回填 |

**数据流。** 只新增 `role="tool"` 消息，内容 `[interrupted]`；不修改 assistant 消息，不改变 id。

**异常处理与回滚。** 不新增异常类型。若 `_answer_pending_tool_calls` 自身抛异常（纯内存操作，概率极低），会覆盖原异常；完整生产化版本用 `try/except Exception` 包住回填并记录日志，最小版本接受该风险。

**向后兼容。** 中断语义不变；`[interrupted]` 是既有契约（`tests/test_safety_matrix.py:184`）。

**验收标准（v1.2 实测结果）。**

- 新增用例 5 条全绿，既有全量用例全绿（`245 passed, 1 skipped`）。
- 手工探针：抛异常后，历史中每个 `tool_call_id` 都有且只有一条回复（附录 A.1 输出 `history valid: True`）。
- `python -m corecoder --demo` 正常完成（退出码 0，offline `ScriptedLLM`）；全量测试中的 demo 用例同时通过。

**完整生产化改动（M1-full）。**

- 将回填函数重命名为 `_ensure_tool_replies()`，并新增公共方法 `Agent.repair_history()`，供 `cli.py:101-112` 恢复旧会话后调用一次。
- `cli.py` 捕获非 `KeyboardInterrupt` 异常时，提示“历史已修复，可继续”，而不是只打印错误。
- 将回填事件写入日志（依赖第 9 节的可观测性议题）。

### 3.d 测试方案

文件：`tests/test_agent_loop_hardening.py`（v1.2 时该文件已存在，M1 用例追加为组 10 `TestExceptionBackfill`，`435-550`，含辅助工具 `ExitTool`）。共享断言：

```python
def assert_tool_calls_answered(messages):
    ids = [tc["id"] for m in messages if m.get("role") == "assistant"
           for tc in (m.get("tool_calls") or [])]
    answers = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
    assert sorted(ids) == sorted(answers)
```

| 用例名（均已实施、已绿） | 构造与失败注入 | 实测断言点 |
|---|---|---|
| `test_on_tool_callback_exception_backfills_then_raises` | `Agent(llm=ScriptedLLM([...]), tools=[SpyTool()])`，`on_tool` 抛 `RuntimeError("UI callback crashed")` | 抛 `RuntimeError`（`match` 校文案）；`assert_tool_calls_answered`；`replies == [("t1", "[interrupted]")]`；`spy.calls == []`（回调先于执行崩溃） |
| `test_permission_ask_exception_backfills_then_raises` | `Permission(ask=raiser)`，raiser 抛 `PermissionError`，工具用 `WriteFileTool`，目标 `tmp_path/a.txt` | 抛 `PermissionError`；`replies == ["[interrupted]"]`；目标文件不存在 |
| `test_tool_baseexception_backfills_then_raises` | 工具 `ExitTool.execute` 抛 `SystemExit(3)`（穿过 `_exec_tool` 的 `except Exception`） | 抛 `SystemExit`；`replies == ["[interrupted]"]` |
| `test_parallel_worker_exception_backfills_whole_batch` | 并行批次两个调用：`spy` + `exiter`；后者在 pool worker 里抛 `SystemExit`，经 `future.result()` 重抛 | 抛 `SystemExit`；`assert_tool_calls_answered`（已执行完的 `t1` 与崩溃的 `t2` 都有回复，即“整批回填”） |
| `test_success_path_backfill_is_noop` | 单工具正常执行后第二轮返回文本 | `chat` 返回 `"done"`；`spy.calls` 恰好 1 次；tool 消息恰好 1 条且内容为 `"executed"`（证明幂等，不重复写） |

运行（Windows cmd，仓库根目录）：

```bat
.venv\Scripts\python.exe -m pytest tests/test_agent_loop_hardening.py -k "backfill" -v
.venv\Scripts\python.exe -m pytest -q
```

**v1.2 实测：** 第一行为 `5 passed, 15 deselected`；第二行为 `245 passed, 1 skipped`。注意：本条旧版给的 `-k "backfill"` 在 v1.1 实际是 0 命中（当时文件里没有名称含 backfill 的用例，命令会报 no tests ran）；v1.2 起 pytest 的 `-k` 对名称大小写不敏感，类名 `TestExceptionBackfill` 也参与匹配，因此现在恰好命中这 5 条。

**TDD 记录（先红后绿）：** 未改 `agent.py` 时先跑组 10，结果为 `4 failed, 1 passed`，四条失败全部是 `dangling: ids=['t1'] answers=[]`（并行用例为 `ids=['t1','t2']`）；唯一通过的正是 `test_success_path_backfill_is_noop`。把 `except KeyboardInterrupt` 换成 `finally` 后重跑，5 条全绿。

说明：`ScriptedLLM` 位于 `corecoder/demo.py:22-45`，脚本轮次用尽会抛 `RuntimeError("ScriptedLLM ran out of turns")`，每个用例只给恰好需要的轮次。

### 3.e 人工验证

**已实测（v1.2，真实模型）。** 仓库自带 `examples/m1_exception_backfill.py`：真 API、真工具、真历史，唯一模拟的是崩溃点（`on_tool` 故意抛异常，模拟库嵌入方/自定义 UI 的回调崩溃）。实施前预期 `history valid: False`，第二轮被 provider 拒绝；实施后实测输出：

```text
chat() raised: simulated UI callback crash while showing read_file(file_path='...\\numbers.txt')
assistant tool_call ids: ['call_5e741b...']
tool replies           : ['call_5e741b...']
history valid          : True
second request succeeded: 'OK'
```

判定依据是 `history valid` 与第二轮是否成功。脚本最后那句 `This provider tolerated the dangling tool_calls.` 是固定说明文案，成功与失败分支都会打印，不能当判定依据。复现命令：

```bat
.venv\Scripts\python.exe examples\m1_exception_backfill.py
```

需要 `.env` 里的 `OPENAI_API_KEY` 与 `CORECODER_MODEL`；`.env` 从仓库根目录显式加载，工作目录不影响。该脚本目前仍在 `.gitignore` 中（`examples/m1_*.py`）；M1 修复已落地，若要让脚本入库，删掉该忽略行后 `git add examples/m1_exception_backfill.py` 即可。

下面的探针不需要 API key，适合离线复核。

前置条件：

- 仓库根目录 `D:\github\CoreCoder`，`.venv\Scripts\python.exe` 存在。
- 不需要 API key（使用 `ScriptedLLM`）。

步骤（cmd）：

1. `cd /d D:\github\CoreCoder`
2. 用记事本创建 `%TEMP%\cc_m1_probe.py`，内容见附录 A.1。
3. `.venv\Scripts\python.exe "%TEMP%\cc_m1_probe.py"`

预期输出（修复后）：

```text
raised: RuntimeError
tool replies: ['t1']
last tool content: [interrupted]
history valid: True
```

修复前同一探针应输出 `tool replies: []`、`history valid: False`；这两组输出可用于确认探针确实覆盖了目标路径。

失败注入：探针已内置 `on_tool` 抛异常；把 `raise RuntimeError` 注释掉再跑，应看到正常工具输出且 `history valid: True`，用于确认探针本身有效。

清理：`del "%TEMP%\cc_m1_probe.py"`

PyCharm：

1. Run/Debug Configurations → Python。
2. Script path 选 `%TEMP%\cc_m1_probe.py` 对应的实际路径。
3. Working directory 必须设为 `D:\github\CoreCoder`，否则 `import corecoder` 失败。
4. 运行，预期输出同上。

## 4. M2 截断响应未识别

### 4.a 问题现状

**先讲后果。** 模型一次能写的字数有上限。如果它在写“工具参数”时被截断，我们收到的是半截 JSON。现在的代码解析失败后不报错，而是当成“这个工具没有参数”继续执行。普通工具会被参数校验拦下，用户看到模型反复说“参数错了”却不知道为什么；MCP 工具则会真的用空参数去调用远端服务，可能删错文件、发错请求。用户感受是“agent 突然做了一个莫名其妙的空操作”。

**术语。** `finish_reason="length"` 表示响应因达到输出上限被截断；半截 JSON 指 `tool_calls[].function.arguments` 在流式拼接后不是合法 JSON。

**触发条件。**

- provider 因 `max_tokens` 截断，截断点落在工具参数中间；或落在合法 JSON 边界但缺失必填字段。
- `_drain` 全程不读取 `finish_reason`（`llm.py:209-277`，已全仓 grep 确认 0 命中）。

**真实执行路径。**

- `_drain` 按 chunk 累积参数字符串（`llm.py:246-257`）→ `json.loads` 失败 → `args = {}`（`263-266`）→ 生成 `ToolCall`（`267`）→ `chat()` 认为有工具调用（`agent.py:84`）→ 写入历史（`90`）→ `_exec_tool`（`99` 或并行 `195`）。
- `_exec_tool` 先做签名绑定（`agent.py:160-163`）：内置工具的必填参数缺失 → `TypeError` → 返回 `Error: bad arguments...`，不执行。
- MCP 工具：`MCPTool.execute(self, **kwargs)`（`mcp.py:206-207`）签名绑定永远成功 → `call_tool(remote_name, {})`（`mcp.py:107-111`）→ 远端真的执行。

**失败表现。**

| 工具类型 | 实际后果 | 用户感知 |
|---|---|---|
| 内置工具（bash/read/write/edit/grep/todo/fetch） | 被 `bind` 拦下，返回 `bad arguments`，不执行 | 模型反复重试同一调用，轮次被烧掉 |
| MCP 工具 | 空参数真实调用远端 | 可能产生副作用；且用户“同意过的那次调用”并不等于“空参数调用” |
| 纯文本被截断（无 tool_calls） | 回答变短，历史合法 | 答案不完整，但没有错误 |

**现有兜底。** `_exec_tool` 的签名绑定（仅对内置有效）；`context.py` 的 snip 与压缩不处理截断；无 `finish_reason` 检查，无截断标记。

**测试缺口。** 无 `finish_reason=length`、无“截断轮不执行”、无 MCP 空参数回归。核实命令见第 10.2 节。

### 4.b 方案决策

**候选方案。**

- A：`LLMResponse` 增加 `finish_reason`，`_drain` 记录；`chat()` 在 `length` 且存在 `tool_calls` 时整轮不执行，回填明确错误，最多自动重试一轮。
- B：只做参数解析严格化：`arguments` 非空且解析失败时标记错误，不生成 `ToolCall`。
- C：调大 `max_tokens` 或换模型（运维手段）。
- D：在 `_exec_tool` 中对 MCP 工具按 `inputSchema.required` 校验空参数。

**对比。**

| 维度 | A finish_reason | B 严格解析 | C 调大上限 | D MCP 校验 |
|---|---|---|---|---|
| 正确性 | 高，权威信号覆盖“合法但缺字段” | 中，无法区分零参数与截断 | 低，只是降低概率 | 中，只保护 MCP |
| 改动范围 | `llm.py` + `agent.py` | `llm.py` 一处 | 配置 | `agent.py` |
| 复杂度 | 低 | 低 | 无 | 低 |
| 兼容性 | 默认值兼容旧构造 | 会误伤零参数工具 | 完全 | 完全 |
| 性能与成本 | 无额外调用 | 无 | 输出 token 成本上升 | 无 |
| 风险 | 需正确识别 provider 的 finish_reason | 破坏零参数工具 | 不解决根本 | 不解决内置工具的无效重试 |

**推荐：A 为主，B 作为补充防线（仅在 `finish_reason == "length"` 时判定），D 作为 MCP 侧可选加固。**

- 决策依据：`finish_reason` 是 provider 的权威截断信号，能覆盖“JSON 合法但字段缺失”的情况；单独用 B 会误伤零参数工具（`json.loads("")` 必然失败，这是正常流）。
- 为什么不选 C：不可控，且不解决“截断后如何自处”。
- 为什么不只选 D：只保护 MCP，内置工具仍会在“参数错误”上反复烧轮次。

**行为设计。**

- `LLMResponse.finish_reason: str = ""`，默认空，兼容 `ScriptedLLM` 与既有测试构造。
- `chat()` 内维护 `truncated_rounds`：
  - `resp.finish_reason == "length"` 且 `resp.tool_calls`：不执行任何工具，按 id 回填 `[truncated by max_tokens; re-issue the call with smaller arguments]`，`continue`。
  - 连续 2 次：返回 `"(model output truncated twice; raise max_tokens or reduce the request)"`，停止循环。
  - 无 `tool_calls` 的截断：保持现状返回文本（可选在 REPL 提示“回答可能不完整”）。

**v1.1 修订提示。** 上表“连续 2 次”的终止方式已由“返回字符串”改为抛异常；4.b 作为补充防线的 B（严格解析）也从“仅在 `length` 时判定”放宽为无条件判定。两处偏离的原因与实现在 4.f。从零实施时请直接按 4.f 的定稿做，不要照 4.c 的伪代码实现。

### 4.c 具体实施

**最小可行改动（M2-min）。**

1. `corecoder/llm.py`
   - `LLMResponse`（`20-33`）增加字段 `finish_reason: str = ""`。
   - `_drain`（`209-277`）增加局部变量，循环内读取 `chunk.choices[0].finish_reason`，非空则记录。
   - 返回 `LLMResponse(..., finish_reason=finish_reason)`。
2. `corecoder/agent.py`
   - `chat()` 在 `84` 的“无工具调用”判断之前插入截断分支；伪代码：

```python
truncated_rounds = 0
for _ in range(self.max_rounds):
    resp = self.llm.chat(...)

    if resp.finish_reason == "length" and resp.tool_calls:
        truncated_rounds += 1
        self.messages.append(resp.message)
        for tc in resp.tool_calls:
            self.messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": "[truncated by max_tokens; re-issue the call with smaller arguments]",
            })
        if truncated_rounds >= 2:
            return "(model output truncated twice; raise max_tokens or reduce the request)"
        continue

    truncated_rounds = 0
    if not resp.tool_calls:
        ...
```

要点：截断轮也必须写入 assistant 消息并回填每个 id，否则历史不合法；不执行任何工具。

**控制流变化。**

| 场景 | 现在 | 改后 |
|---|---|---|
| 完整响应 | 正常执行 | 不变 |
| 截断且有工具调用 | 空参数执行 | 不执行，回填错误，继续下一轮 |
| 连续两次截断 | 继续空参数执行 | 返回明确终止文本 |
| 零参数工具（完整响应） | `args={}` 正常执行 | 不变（`finish_reason` 不是 length） |

**异常与回滚。** 纯内存字段，无持久化格式变化；回滚只需 revert `llm.py` 与 `agent.py`。

**向后兼容。** dataclass 默认值保证旧构造方式可用；`test_litellm.py` 的假 chunk 没有 `finish_reason`，`getattr` 返回 `None`，不影响。

**验收标准。** 新增 5 条用例全绿；既有全量用例全绿；探针显示截断轮工具执行次数为 0。

**完整生产化改动（M2-full）。**

- `LLMResponse` 增加 `truncated` 属性；REPL 对文本截断显示提示。
- MCP 侧增加 `required` 校验，缺字段直接返回错误而不是调用远端。
- 记录每次截断的轮次与模型名，为后续预算与可观测性铺路。

### 4.d 测试方案

| 用例名 | 构造与失败注入 | 断言点 |
|---|---|---|
| `test_drain_records_finish_reason_length` | Mock client 返回含 `finish_reason="length"` 的 chunk（参考 `tests/test_core.py:546-554` 的 `SimpleNamespace` 风格） | `resp.finish_reason == "length"` |
| `test_truncated_tool_call_is_not_executed` | `ScriptedLLM` 第一轮 `LLMResponse(finish_reason="length", tool_calls=[...])`，spy 工具记录调用；第二轮文本 | spy 调用次数 0；tool 回复含 `truncated`；`assert_tool_calls_answered` |
| `test_two_consecutive_truncations_stop_the_loop` | 两轮都是 length + tool_calls | 返回值包含 `truncated twice`；工具未执行 |
| `test_zero_arg_tool_with_empty_arguments_still_runs` | `finish_reason="stop"`，`ToolCall(arguments={})` | spy 执行 1 次（防 B 方案误伤） |
| `test_text_truncation_without_tool_calls_returns_content` | length 但无 tool_calls | 返回文本；历史只有 assistant 消息 |

运行：

```bat
.venv\Scripts\python.exe -m pytest tests/test_agent_loop_hardening.py -k "truncat" -v
```

### 4.e 人工验证

前置条件：同 M1，不需要 API key。

步骤（cmd）：

1. `cd /d D:\github\CoreCoder`
2. 创建 `%TEMP%\cc_m2_probe.py`，内容见附录 A.2。
3. `.venv\Scripts\python.exe "%TEMP%\cc_m2_probe.py"`

预期输出（修复后）：

```text
tool executed: 0
tool reply: [truncated by max_tokens; re-issue the call with smaller arguments]
answer: done
```

修复前同一探针会因 `LLMResponse` 缺少 `finish_reason` 参数而直接 `TypeError`，这本身说明字段尚未实现。

失败注入：把第一轮的 `finish_reason="length"` 改为 `""` 再跑，应看到 `tool executed: 1`，证明探针能区分两条路径。

清理：`del "%TEMP%\cc_m2_probe.py"`

PyCharm：Script path 指向 `%TEMP%\cc_m2_probe.py`，Working directory 设为仓库根目录；预期输出同上。

### 4.f 实施复盘：M2 暴露的两个必须修问题（v1.1）

M2 按 4.c 实施后，对同一批改动做了两轮生产级复核，判定出两项必须修的次生问题（下列 P1 指复核严重度，与第 2 节的优先级无关，见 0.5）。两项都属于 M2 的直接产物：R1 是因为 4.b 把严格解析降级成了“仅在 length 时生效”，低估了非截断成因的坏参数；R2 是因为 4.b 选的终止通道（返回值）在真实调用链上根本传不出去。

#### 4.f.1 R1：非 length 的坏参数仍被空参数执行（P1）

**先讲后果。** 4.c 的截断闸门只认 `finish_reason == "length"`。但“参数串解析失败”还有别的成因：provider 不发这个字段，或者模型自己吐出了坏 JSON，与截断无关。这时闸门不介入，调用照旧走到 `_exec_tool`；而 `_exec_tool` 只靠 `inspect.signature.bind` 拦截，MCP 工具的 `execute(**kwargs)` 绑定永远成功，于是带着默认值真的调用了远端。换句话说，M2 修掉了“因截断而空参数执行”，没修掉“因坏 JSON 而空参数执行”，两者后果完全一样。

**术语。** `arguments_error`：`ToolCall` 上的标记字段（`llm.py:27-29`），非空表示参数串解析失败或不是 JSON 对象；`raw_arguments`（`llm.py:25-26`）保留流式收到的原文。防线有两道：第一道在执行前短路（单条路径与并行路径各一处），第二道在 `_exec_tool` 内部，全仓唯一的 `tool.execute(` 调用点前。

**触发条件。**

| 成因 | finish_reason | 4.c 的闸门 | v1.1 结果 |
|---|---|---|---|
| `max_tokens` 截断落在参数中间 | `length` | 介入，整轮不执行 | 不执行（M2 本体） |
| provider 不发 finish_reason（部分网关、LiteLLM 方言） | `""` | 不介入 | 按 `arguments_error` 拒绝 |
| 模型自己吐出坏 JSON | `stop` / `tool_calls` | 不介入 | 按 `arguments_error` 拒绝 |
| 合法 JSON 但不是对象（数组、`null`、数字） | 任意 | 不介入 | 按 `arguments_error` 拒绝 |
| 零参数工具（`arguments: ""`） | 任意 | 不介入 | 正常执行，不误伤 |

**真实执行路径（v1.1）。**

- 解析段 `_drain`（`llm.py:270-292`）：空串 → `arguments={}`；解析失败或非 dict → `arguments_error=raw_args`，原文一并带出。
- 单条路径 `_execute_round`（`agent.py:153-169`）：`self._bad_arguments(tc) or self._pre_hooks(tc) or self._permit(tc)`（`159-161`）。
- 并行路径 `_exec_tools_parallel`（`agent.py:247-295`）：同一短路表达式（`263-265`）；被拒的调用不进线程池，也不触发 post-hook（`293-294` 只对已执行的下标回放）。
- 第二道防线 `_exec_tool`（`agent.py:225-234`）：未知工具名判定之后再判一次，覆盖并行 worker 与其他一切到达执行的路径。

**为什么必须先于 hooks 与权限。** 短路点放在 `_pre_hooks` / `_permit` 之前有两个理由：一是被拒调用没有执行，不该触发 post-hook；二是如果让权限确认先跑，用户会被要求批准一次参数已知损坏的调用，而他“同意的那次调用”与模型“真正想做的那次调用”已经不是一回事了。

**实施要点。**

- 用 `or` 短路而不是 `if` 串联，把“坏参数”排在 hooks 与权限之前；`or` 链里空字符串返回值仍按 falsy 继续往下走，不改变原有语义。
- 拒绝文案以 `Error: bad arguments for ...` 开头，与 `_exec_tool` 既有的 bind 失败文案（`agent.py:239-240`）同形，模型侧不必区分“参数没解析出来”和“参数缺字段”两种提示。
- 原始参数串截到 200 字符再回显（`agent.py:191`），避免一段坏 JSON 把上下文撑大。

**回归测试。** 组 6（`tests/test_agent_loop_hardening.py:236-312`）四条：坏 JSON 不执行且工具回复含 `not a valid JSON object`；非对象 JSON 拒绝；空参数串不误伤零参数工具；坏参数不进 hooks。

#### 4.f.2 R2：截断终止信号只经返回值传出（P1）

**先讲后果。** 4.b 定的终止方式是 `return "(model output truncated twice; ...)"`。这个字符串在三个调用方都到不了用户眼前：REPL 只在流式内容为空时才拿返回值渲染，而截断场景下 `streamed` 非空，返回值被直接丢弃；one-shot 模式丢弃返回值后落到结尾的 `print()`，正常退出 0；子 agent 的包装层只捕获异常，返回值会被当成“子任务的结论”包装成 `[Sub-agent completed]` 交回父 agent。结果是：一次确定失败的任务，分别表现成“屏幕上什么都没说”“脚本判成功”“父 agent 以为干完了”。

**术语。** 失败信号的传递通道：返回值会被上层丢弃，异常会被逐层捕获，两者不等价。`TruncatedOutput` 是 `RuntimeError` 的子类（`agent.py:25-29`），且只在历史已回填合法之后抛出。

**三个调用方的现状与接线。**

| 调用方 | 代码位置 | 只收返回值时 | v1.1 接线 |
|---|---|---|---|
| 一次性模式 | `cli.py:154-167` | 丢弃后 `print()`，退出 0 | `except TruncatedOutput` → stderr 打印 + `sys.exit(1)`（`159-162`） |
| 交互式 REPL | `cli.py:324-339` | 丢弃，屏幕上无提示 | 打印原因并继续对话（`334-336`），历史合法，可 `/save` 或重试 |
| 子 agent | `tools/agent.py:68-69` | 包装成 `[Sub-agent completed]` | 被既有 `except Exception` 捕获，降级为 `Sub-agent error: ...` 文本 |

**为什么必须抛在回填之后。** 抛出点在 `_answer_truncated_tool_calls`（`agent.py:136-148`）执行完毕之后，所以历史里每个 `tool_call_id` 都有回复，异常逃逸后用户仍能 `/save`、重试或继续对话。这是 4.c 里“截断轮也必须写入 assistant 消息并回填每个 id”那条不变量的延续，不是新约定。

**连带修正 R5（轮次触顶）。** 循环出口处新增判定（`agent.py:126-133`）：若最后一轮是截断则仍抛 `TruncatedOutput`，否则 `max_rounds=1` 这类配置会退回裸字符串返回，重新制造本条要消灭的静默失败。纯触顶（非截断）仍按原样返回 `(reached maximum tool-call rounds)`，残留风险见第 9 节。

**连带修正 R4（并行覆盖真空）。** 组 1–7 全部是单条 `tool_call` 的轮次，走的都是 `_execute_round` 的 `len == 1` 分支，`_exec_tools_parallel` 里新增的短路表达式从未被任何用例执行过——新加的防线自己无人守着。补组 8 `TestParallelBatch`（`tests/test_agent_loop_hardening.py:378`）：同一轮混合一个合法调用与一个坏 JSON 调用，断言合法的执行、坏的不执行、pre/post hook 只对合法调用触发、tool 回复按 id 对齐。其中 `pre == ["good"]` 是关键断言——它把并行分支的短路与 `_exec_tool` 的第二道防线区分开，删掉前者用例即红。

**回归测试与现状。** `tests/test_agent_loop_hardening.py` 现为 12 组（组 10 为 v1.2 追加的 M1 用例，组 11 为 v1.3 追加的 M3 用例，组 12 为 v1.4 追加的 M4 用例）：

| 组 | 位置（类定义起始行，v1.2 实测） | 覆盖 |
|---|---|---|
| 1 | `98` | `finish_reason` 传播（length / stop） |
| 2 | `142` | 截断轮不执行、按 id 回填 |
| 3 | `171` | 连续两次截断抛 `TruncatedOutput`（原 4.d 的 `stop_the_loop` 用例已改为抛异常断言） |
| 4 | `199` | 零参数工具不误伤 |
| 5 | `221` | 无 tool_calls 的文本截断 |
| 6 | `239` | R1 坏参数防线 |
| 7 | `319` | R2：one-shot 退出非 0、REPL 存活、子 agent 不谎报完成 |
| 8 | `378` | R4 并行混合批次 |
| 9 | `419` | R5 `max_rounds=1` 下截断仍抛 |
| 10 | `452` | M1（v1.2）：逃逸异常回填（`on_tool` 崩溃 / `ask` 崩溃 / 工具抛 `BaseException` / 并行整批 / 成功路径 no-op） |
| 11 | `590` | M3（v1.3）：context 400 强制压缩并重试一次、第二次失败原样抛、非 context 400 不重试、短历史不压缩、固定开销计入 snip 阈值、agent 传参 |
| 12 | `717` | M4（v1.4）：长结果封顶、头尾保留、四类密钥脱敏、`read_file` 超大文件拒绝、MCP 形状结果封顶、并行结果独立封顶 |

全量 `pytest -q` 为 **257 passed / 1 skipped**（v1.4 实测，含组 12 的 6 条；v1.3 记为 251 passed / 1 skipped，差额即本次新增用例）；改动文件 `ruff check` 通过。附录 A.2 的探针仍有效，但它只验证 M2 本体（截断轮不执行），不覆盖 R1/R2；复核 R1/R2 请直接跑上述用例。

## 5. M3 上下文超限无兜底

> **状态（v1.3）：已实施。** 落点：`corecoder/llm.py:108-120`（`is_context_length_error`）、`corecoder/context.py:56-86`（`fixed_tokens` 与 `force_collapse`）、`corecoder/agent.py:78-82,84-107,149`（固定开销估算 + 请求级兜底）。用例组 11（6 条，`tests/test_agent_loop_hardening.py:590`）。实测：组 11 `6 passed`，全量 `251 passed / 1 skipped`，`ruff` 通过；真实 provider 脚本见 5.e。5.a 的问题行号是修复前存档，当前行号以本状态头与 5.c 为准。

### 5.a 问题现状

**先讲后果。** agent 判断“上下文快满了”时，只数了对话消息，没有数系统提示词和工具说明书。就像装箱只量了衣服，忘了隔板，以为还剩 10% 空间，实际已经超了。一旦超限，API 直接拒绝，界面只打印一行错，本轮白干，而且不会自动压缩重来。用户感受是：长会话聊到某个点突然开始每句都报 `maximum context length`，只能 `/compact` 或 `/reset` 丢历史。

**术语。** `context_length_exceeded`：请求的 token 总数超过模型窗口。固定开销（fixed overhead）：每次请求都带的 system prompt、plan 提示、todo 渲染和 tool schema，它们不随对话增长，但也不在 `self.messages` 里。

**触发条件。**

- `maybe_compress(self.messages, ...)` 只估算 `self.messages`（`agent.py:73,122`；`context.py:58`）。
- 真实请求 = `_full_messages()`（system + plan + todo，`agent.py:53-65`）+ `_tool_schemas()`（`67-68`）+ messages。
- 压缩阈值：snip 50%、summarize 70%、collapse 90%（`context.py:52-54`）。未计入的固定开销足以把 90% 推过 100%。

**真实执行路径。**

1. 每轮工具执行后调用 `maybe_compress`（`agent.py:122`），估算没过阈值 → 不压缩。
2. 下一轮 `llm.chat` 发出真实请求（`agent.py:76-81`）。
3. provider 返回 400，消息含 `maximum context length`。
4. `_adapt_rejected_param` 只匹配带引号的 `'max_tokens'` / `'temperature'`（`llm.py:112-117`），不匹配 → 先 pop `stream_options` 重试（`188-190`，白费一次往返）→ 再 `raise`（`189`）。
5. `agent.py:76` 没有 try → 异常抛出 `chat()` → `cli.py:330-332` 打印错误并继续，历史保留。
6. 用户再发一句，历史仍在，仍然超限 → 再次失败。

**失败表现。**

- 用户看到 `Error: Error code: 400 ... maximum context length ...`。
- 无自愈：不会强制压缩重试。
- `/compact` 使用同一套估算（`cli.py:256-265`），可能压完仍超；`/reset` 能恢复但丢历史。

**现有兜底。** 三层压缩（`context.py:56-76`）；`_get_summary` 失败时回退到关键词抽取（`159-186`）；`_hard_collapse` 用 `_safe_split` 避免孤儿 tool 消息（`106-116,142-157`）。没有“请求被拒后压缩重试”的路径。

**测试缺口。** 无 context-length 用例；无“固定开销计入估算”用例。核实命令见第 10.2 节。

### 5.b 方案决策

**候选方案。**

- A：请求级兜底：捕获 context-length 类 400 → 强制 hard collapse → 重试一次。
- B：修估算：`maybe_compress` 计入 system prompt 与 tool schema。
- C：引入真实 tokenizer（tiktoken）精确计数。
- D：把阈值从 90% 降到 70%，预留更大 buffer。

**对比。**

| 维度 | A 请求级兜底 | B 修估算 | C 真实 tokenizer | D 降阈值 |
|---|---|---|---|---|
| 正确性 | 高，最后防线 | 中，降低概率但不保证 | 高，但各 provider 不一致 | 低，只是更早压缩 |
| 改动范围 | `agent.py` + `context.py` + `llm.py` | `agent.py` + `context.py` | 依赖 + 多处 | 一行 |
| 复杂度 | 低 | 低 | 中高 | 极低 |
| 兼容性 | 完全 | 完全（新增默认参数） | 新增依赖 | 完全 |
| 性能与成本 | 失败时多一次压缩调用 | 每轮多一次 schema 序列化与估算 | 每次请求精确编码，CPU 成本 | 更频繁压缩，摘要成本上升 |
| 风险 | 重试仍失败需正确重抛 | 估算仍是近似 | provider 方言差异 | 浪费可用上下文，仍可能超 |

**推荐：A + B。**

- A 是最后防线：provider 的 tokenizer 与我们的估算永远不可能完全一致，必须有“被拒后自救”的路径。
- B 是根因缓解：让 90% 阈值真正代表“请求总 token 的 90%”，而不是“消息的 90%”。
- 为什么不用 C：新增依赖，且不同 provider 的 tokenizer 不同，收益不足以覆盖复杂度。
- 为什么不用 D：只是把问题推迟，且压缩更频繁、成本更高。

**行为设计。**

- `llm.py` 新增 `is_context_length_error(exc) -> bool`：大小写不敏感匹配 `maximum context length`、`context_length_exceeded`、`reduce the length of the messages`、`too many tokens`。
- `context.py` 新增公共方法 `force_collapse(messages, llm=None)`，内部调用现有 `_hard_collapse`。
- `maybe_compress(messages, llm=None, fixed_tokens=0)`：`current = estimate_tokens(messages) + fixed_tokens`。
- `agent.py` 在 `76` 处包一层：仅对 context-length 且历史长度大于 4 的情况强制压缩并重试一次；其它 400 原样抛出。

### 5.c 具体实施

**已实施内容（M3-min，v1.3 实测）。**

1. `corecoder/llm.py:108-120`
   - 新增 `_CONTEXT_ERROR_HINTS`（`maximum context length` / `context_length_exceeded` / `reduce the length of the messages` / `too many tokens`）与 `is_context_length_error(exc) -> bool`，`str(exc).lower()` 后做包含匹配，大小写不敏感，同时覆盖 OpenAI SDK 与 LiteLLM 的异常类型。

2. `corecoder/context.py:56-86`
   - `maybe_compress(self, messages, llm=None, fixed_tokens: int = 0)`：`current = estimate_tokens(messages) + fixed_tokens`，每次压缩后重算时同样加 `fixed_tokens`。
   - 新增 `force_collapse(self, messages, llm=None)`，直接调用现有 `_hard_collapse`（`context.py:152`），复用 `_safe_split`，不会产生孤儿 tool 消息。

3. `corecoder/agent.py:78-82,84-107,149`
   - 新增 `_fixed_prompt_tokens()`：`_full_messages()[0]["content"]` 的估算 + `json.dumps(self._tool_schemas())` 的估算，即固定开销。
   - 两处 `maybe_compress` 调用（`agent.py:87-88,148-149`）都传 `fixed_tokens=self._fixed_prompt_tokens()`。
   - 第一次 `llm.chat`（`agent.py:93-106`）包 `try/except Exception`：仅当 `is_context_length_error(e)` 且 `len(self.messages) > 4` 时 `force_collapse`，再用**重新生成的** `_full_messages()` 与 `_tool_schemas()` 重试一次；其余异常与第二次失败都原样抛出。捕获 `Exception` 而非 `BadRequestError`，是为了同时覆盖两条后端；`KeyboardInterrupt` 属于 `BaseException`，不会被吞。

**关键实现细节。**

- 重试时必须重新调用 `_full_messages()`：压缩改写了 `self.messages`，复用压缩前的列表会发出旧历史。
- `len(self.messages) <= 4` 直接抛：历史太短时 `_hard_collapse` 无内容可丢，重试必然同样失败。
- 只重试一次，不做循环；`_answer_pending_tool_calls` 与 `cli.py` 的 `/compact` 均未改动。`/compact` 仍按旧估算（不计固定开销），作为残留限制记录在此。

**向后兼容。** `fixed_tokens` 默认 0，旧调用行为不变；`force_collapse` 是新增公共方法，不改变既有签名。

**回滚点。** `git revert <M3 commit>`，同时回滚 `corecoder/llm.py`、`corecoder/context.py`、`corecoder/agent.py`、组 11 与 `examples/m3_context_overflow.py`。

**未纳入 M3-full。** `ContextManager.measure_request()` 三段明细、provider 400 后不再白丢 `stream_options`、压缩前后 token 计数接入日志，均属可观测性/省一次往返，本轮不做（见第 9 节）。

### 5.d 测试方案

组 11 `TestContextOverflowRecovery`（`tests/test_agent_loop_hardening.py:590`）共 6 条。构造 `BadRequestError` 的辅助函数沿用了 `tests/test_core.py` 的 `httpx`/`httpx2` 二选一写法；fake LLM 记录 `chat` 次数，`_get_summary` 用 `mock.patch.object(..., return_value="summary")` 隔离摘要请求，压缩计数用 `mock.patch.object(agent.context, "_hard_collapse", wraps=...)`（不依赖 `force_collapse` 是否存在，确保先红时失败原因是行为而不是 `AttributeError`）。

| # | 用例名 | 构造与失败注入 | 断言点 | 先红时的实际失败原因 |
|---|---|---|---|---|
| 1 | `test_context_overflow_triggers_force_collapse_and_retries` | 第 1 次抛 context 400，第 2 次返回 `"recovered"` | 返回 `"recovered"`；`_hard_collapse` 1 次；`llm.calls == 2` | `BadRequestError: maximum context length` 直接冒出（无兜底） |
| 2 | `test_context_overflow_retries_exactly_once` | 两次都抛 context 400，其中一条用大写 `MAXIMUM CONTEXT LENGTH` | 第二次异常外抛；`_hard_collapse` 1 次；`llm.calls == 2` | 异常在第一次就冒出（`calls == 1`、collapse 0） |
| 3 | `test_non_context_400_is_not_retried_at_agent_level` | `BadRequestError("Unsupported parameter: 'response_format'")` | 直接抛；`_hard_collapse` 0 次；`llm.calls == 1` | 已绿（回归护栏） |
| 4 | `test_short_history_overflow_is_not_collapsed` | 预置 2 条消息（chat 后 3 条 ≤ 4），抛 context 400 | 直接抛；`_hard_collapse` 0 次 | 已绿（回归护栏） |
| 5 | `test_compression_counts_fixed_overhead` | `ContextManager(max_tokens=2000)`，一条 10 行 × 200 字符的 tool 消息（约 591 tokens，低于 snip 阈值） | 无 `fixed_tokens` 时 `maybe_compress` 返回 `False`；`fixed_tokens=500` 时返回 `True` 且 tool 内容出现 `snipped` | `TypeError: unexpected keyword argument 'fixed_tokens'` |
| 6 | `test_agent_counts_fixed_overhead_in_compression` | `Agent(llm=ScriptedLLM([...]), tools=[SpyTool()])`，patch `agent.context.maybe_compress` | 调用参数 `kwargs["fixed_tokens"] > 0` | `KeyError: 'fixed_tokens'`（agent 未传） |

**先红实测（v1.3）：4 红 2 绿**——#1、#2、#5、#6 因上表原因失败，#3、#4 作为回归护栏一开始就是绿的。

运行：

```bat
.venv\Scripts\python.exe -m pytest tests/test_agent_loop_hardening.py -k "context or fixed_overhead" -v
```

**先绿实测（v1.3）**：`6 passed, 20 deselected`；随后全量 `251 passed, 1 skipped`（24.27s），`ruff check corecoder tests` 输出 `All checks passed!`。

### 5.e 人工验证

离线探针仍保留在附录 A.3（不依赖 API key）。v1.3 增加了真实 provider 脚本 `examples/m3_context_overflow.py`（已用 `git add -f` 入库，`.gitignore` 规则保持不变），它用真实的 system prompt 与 tool schema 作为固定开销，历史首条约 1.45M 估算 token。

真实运行（仓库根目录，显式加载 `.env` 的 `OPENAI_API_KEY` / `CORECODER_MODEL`）：

```bat
.venv\Scripts\python.exe examples\m3_context_overflow.py
.venv\Scripts\python.exe examples\m3_context_overflow.py --inject-400
```

**实测 1（不注入，真实 provider）**：本机 provider（`mimo-v2.6-flash`，`https://api.xiaomimimo.com/v1`）**接受了 1,455,907 估算 token 的请求并正常回答**，未触发 400，脚本按设计退出 2 并提示改用注入：

```text
fixed prompt tokens (system + tool schemas): 2092
history tokens (before the new prompt): 1455907
context rejection: False
collapse calls: 0
chat calls: 1
answer: 'OK<|im_end|>---'
history valid: True
messages after: 10
```

**实测 2（`--inject-400`，注入两次以穿透 `stream_options` 回退，恢复后的重试仍是真实 API）**：

```text
context rejection: True
injected 400: True
collapse calls: 1
chat calls: 2
answer: 'OK<|im_end|>black'
history valid: True
messages after: 7
PASS the provider rejection was absorbed by one forced collapse and one retry.
```

退出码与失败注入：0 恢复成功；1 未恢复或计数不符；2 缺 key、provider 宽容接受（未注入时）或模型未按指令回答。把 `seed_messages()` 的大块历史去掉后重跑，则不应出现 400 或压缩。脚本不写文件，无需清理。

## 6. M4 工具结果无统一治理

> **状态（v1.4）：已实施。** 落点：`corecoder/agent.py:43,49`（`max_tool_result_chars`）、`agent.py:253-269`（`_exec_tool` 包装与 `_govern_result`）、`agent.py:371-383`（`_REDACTIONS`/`_redact`）、`corecoder/tools/read.py:8,41-48`（`_MAX_FILE_BYTES` 与大小检查）。用例组 12（6 条，`tests/test_agent_loop_hardening.py:717`）。实测：组 12 `6 passed`，全量 `257 passed / 1 skipped`，`ruff` 通过，`--demo` 退出 0；真实 provider 脚本见 6.e。6.a 的问题行号是修复前存档，当前行号以本状态头与 6.c 为准。

### 6.a 问题现状

**先讲后果。** 工具输出是原样塞回对话的。不同工具自己截断的长度不一样，有的干脆不截：`read_file` 只限制“2000 行”，但一行可以很长，文件还会被整个读进内存；MCP 返回多少就收多少。于是一次读大文件或一次远端调用就能把上下文塞满，用户看到 agent“突然失忆”（其实是压缩把早期对话摘掉了）。另外，命令输出里如果带 API key，会原样进入历史，`/save` 还会写到磁盘。

**术语。** 工具结果治理（tool result governance）：在结果进入历史之前统一做三件事——封顶（clamp）、脱敏（redaction）、标记（marker，告知模型内容被截断）。

**触发条件。**

| 入口 | 代码位置 | 现状 |
|---|---|---|
| read_file | `read.py:34-53` | 只限 2000 行，无字符上限；`read_text` 全量读入内存 |
| MCP | `mcp.py:107-119` | 返回文本无任何大小上限 |
| bash | `bash.py:101-113` | 截断到 15k，但不脱敏 |
| fetch | `fetch.py:37,42` | 读 1MB、截断到 8k |
| edit | `edit.py:96-98` | diff 截断到 2.5k |
| 子 agent | `tools/agent.py:64-66` | 结果截断到 5k |
| grep | `grep.py:64` | 最多 200 条匹配 |

**真实执行路径。** `_exec_tool`（`agent.py:153-168`）原样返回工具文本 → `chat()` 写入 `messages`（`101-105` 或 `109-114`）→ 下一轮 `_full_messages()` 全量发送。中间没有任何统一治理。

**失败表现。**

- 单条 tool 消息过大 → 估算迅速越过 50%/70%/90% 阈值 → snip 或摘要触发，早期对话被压缩，用户感知为“失忆”。
- 极端情况下直接触发 M3 的 context 超限。
- 密钥或令牌进入历史，`/save` 落盘（`cli.py:266-269` 调用 `save_session`）。
- `context.py` 的 snip 是事后补救（`78-103`），且会原地改写历史（`101`），原始内容永久丢失。

**现有兜底。** 上表各工具的自截断；`context.py` 的分层压缩；没有统一入口上限，没有脱敏。

**测试缺口。** 无“长结果封顶”“密钥脱敏”“read_file 大文件拒绝”“MCP 结果封顶”用例。核实命令见第 10.2 节。

### 6.b 方案决策

**候选方案。**

- A：在 loop 层统一治理：`_exec_tool` 出口调用 `_govern_result()`（clamp + redact）。
- B：各工具自己治理。
- C：只依赖 `context.py` 的 snip。
- D：引入结构化 `ToolResult`（text/size/truncated/redactions），由渲染层统一。

**对比。**

| 维度 | A loop 层治理 | B 各工具自管 | C 只靠 snip | D 结构化结果 |
|---|---|---|---|---|
| 正确性 | 高，单点覆盖所有工具 | 中，口径难统一，MCP 不可控 | 低，事后且破坏历史 | 最高，可审计 |
| 改动范围 | `agent.py` + `read.py` | 每个工具 | 无 | 工具基类 + 所有工具 + loop |
| 复杂度 | 低 | 中 | 无 | 高 |
| 兼容性 | 高（短结果不变） | 高 | 高 | 中（接口变化） |
| 性能与成本 | 每条结果一次正则扫描 | 分散 | 无 | 序列化与渲染成本 |
| 风险 | 脱敏误伤或漏检；post-hook 看到治理后文本 | 遗漏新工具 | 上下文已污染才处理 | 超出本轮范围 |

**推荐：A（本轮），D 作为后续演进。**

- 决策依据：`_exec_tool` 是两条执行路径唯一的共同出口（单工具 `99`、并行 `195`），在这里治理能保证“进入历史的一定经过治理”。
- 为什么不用 B：新工具和 MCP 都会绕开；口径无法保证一致。
- 为什么不用 C：snip 发生时内容已经进入上下文，且会原地改写历史、不可逆。
- 为什么不用 D：需要改工具基类与所有工具，属于架构级改造，应单独立项。

**治理规则设计。**

- 封顶：默认 15000 字符（与 bash 现有口径一致），超出时保留头部 2/3、尾部 1/3，中间插入 `... (truncated: N chars total, showing first X and last Y) ...`。
- 脱敏（保守，宁可漏检不误伤代码）：

| 类型 | 正则 | 替换 |
|---|---|---|
| OpenAI 风格 key | `sk-[A-Za-z0-9_-]{16,}` | `[REDACTED:api_key]` |
| Bearer token | `(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}` | `Bearer [REDACTED]` |
| AWS access key | `AKIA[0-9A-Z]{16}` | `[REDACTED:aws_key]` |
| PEM 私钥 | `-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----` | `[REDACTED:private_key]` |

- 顺序：先封顶再脱敏，避免截断把密钥切成两半导致漏检。

### 6.c 具体实施

**已实施内容（M4-min，v1.4 实测）。**

1. `corecoder/agent.py:43,49`
   - `Agent.__init__` 新增 `max_tool_result_chars: int = 15_000` 并保存为实例属性；默认值与 bash 现有口径一致。

2. `corecoder/agent.py:253-269`
   - 原 `_exec_tool` 改名为 `_exec_tool_raw`（未知工具、坏参数、bind 失败、工具异常、正常结果五个返回点保持不变）。
   - 新 `_exec_tool` 只做一层包装：`return self._govern_result(self._exec_tool_raw(tc))`。因此单条路径与并行路径（`_exec_tools_parallel` 的 worker 调用的仍是 `_exec_tool`）的所有出口都自动经过治理。
   - `_govern_result`：超过 `max_tool_result_chars` 时保留头 `limit * 2 // 3`、尾 `limit - head`，中间插入 `... (truncated: N chars total, showing first X and last Y) ...`；先封顶再脱敏，避免截断把密钥切成两半导致漏检。

3. `corecoder/agent.py:371-383`
   - 模块级 `_REDACTIONS`（四条正则）与 `_redact(text)`，按顺序替换：OpenAI key、Bearer、AWS key、PEM 私钥。规则与 6.b 的设计一致；未做 `try/except` 回退，因为正则是静态的，不存在运行期编译失败。

4. `corecoder/tools/read.py:8,41-48`
   - 模块常量 `_MAX_FILE_BYTES = 10 * 1024 * 1024`。
   - `execute` 在 `is_file()` 之后、`read_text` 之前检查 `p.stat().st_size`，超限返回 `Error: file too large (...)` 并提示改用 `offset/limit` 或 grep。这样超大文件不会被整个读进内存。

**控制流与数据流。** 工具原始输出 → `_govern_result`（封顶 → 脱敏）→ 写入历史。post-hook 收到的是治理后的文本，这是 M4 的安全优先取舍；原文审计需要 M4-full 的 `ToolResult.raw_text`，仍留在第 9 节。

**未改动。** `mcp.py` 不做单独封顶：治理在 loop 出口统一完成，MCP 与其他工具走同一条路径。`bash/fetch/edit/grep/子 agent` 的各自上限保留，作为更早的一道收敛。

**向后兼容。** 默认上限 15000；短结果逐字节不变；既有 `Edited` / `Wrote` / `Error` 前缀断言不受影响；`max_tool_result_chars` 是新增关键字参数，放在参数表末尾。

**回滚点。** `git revert <M4 commit>`，同时回滚 `corecoder/agent.py`、`corecoder/tools/read.py`、组 12 与 `examples/m4_tool_result_governance.py`。

**未纳入 M4-full。** 结构化 `ToolResult`（`raw_text` 供审计）、MCP `_read_loop` 的单行上限、可配置脱敏规则与命中计数，均属架构级/可观测性改动，本轮不做（见第 9 节）。

### 6.d 测试方案

组 12 `TestToolResultGovernance`（`tests/test_agent_loop_hardening.py:717`）共 6 条，全部走真实 `Agent.chat()` 与真实 `_exec_tool`，用 `FixedOutputTool` / `McpShapedTool` 控制返回内容。

| # | 用例名 | 构造与失败注入 | 断言点 | 先红时的实际失败原因 |
|---|---|---|---|---|
| 1 | `test_long_result_is_clamped_before_history` | 工具返回 `"x" * 100_000` | tool 消息长度 ≤ 15200；含 `truncated:` | `assert 100000 <= 15200` |
| 2 | `test_clamp_preserves_head_and_tail` | 返回 `"HEAD\n" + "x"*100_000 + "\nTAIL"` | 以 `HEAD` 开头、以 `TAIL` 结尾、含 `truncated:` | 头尾成立，但没有 `truncated:` |
| 3 | `test_secret_is_redacted_in_tool_message` | 返回 OpenAI key、`Bearer abcdefghij`、AWS `AKIA...`、PEM 私钥 | 四个原始串均不在历史；`[REDACTED` 出现 4 次 | 原始串全在，无 `[REDACTED` |
| 4 | `test_read_file_rejects_oversized_file` | monkeypatch `read._MAX_FILE_BYTES = 1024`，写 2048 字节 | 返回含 `Error: file too large` | 返回了文件内容（常量未被使用） |
| 5 | `test_mcp_tool_result_is_clamped` | `execute(**kwargs)` 形状的 `mcp__fake__big` 返回 100_000 字符 | 同 #1 | `assert 100000 <= 15200` |
| 6 | `test_parallel_results_are_clamped_independently` | 同一轮两个调用各返回 100_000 字符 | 两条 tool 消息都 ≤ 15200 且都含 marker | 两条都是 100000，无 marker |

**先红实测（v1.4）：6 红 0 绿**，失败原因与上表一致。

运行：

```bat
.venv\Scripts\python.exe -m pytest tests/test_agent_loop_hardening.py -k "clamp or redact or oversized" -v
```

**先绿实测（v1.4）**：`6 passed, 26 deselected`；随后全量 `257 passed, 1 skipped`（24.49s），`ruff check corecoder tests examples/m4_tool_result_governance.py` 输出 `All checks passed!`；`python -m corecoder --demo` 退出码 0。

### 6.e 人工验证

离线探针仍保留在附录 A.4（不依赖 API key）。v1.4 增加了真实 provider 脚本 `examples/m4_tool_result_governance.py`（已用 `git add -f` 入库，`.gitignore` 规则保持不变），分三段：

1. 真实 `read_file` 读取 600,037 字节文件，要求模型实际调用工具；检查封顶、marker、头尾保留。
2. 真实 `read_file` 读取含四类假密钥的文件；检查原始串全部消失、`[REDACTED` 出现。
3. 不调用 API，直接用 `ReadFileTool` 读 11 MB 文件；检查返回 `Error: file too large`。

运行（仓库根目录，显式加载 `.env` 的 `OPENAI_API_KEY` / `CORECODER_MODEL`）：

```bat
.venv\Scripts\python.exe examples\m4_tool_result_governance.py
.venv\Scripts\python.exe examples\m4_tool_result_governance.py --max-result-chars 1000000
.venv\Scripts\python.exe examples\m4_tool_result_governance.py --no-redact
```

**实测 1（默认）**：

```text
stored length: 15076
contains truncated marker: True
head preserved: True
tail preserved: True
raw secret markers in history: 0/4
redacted marker present: True
oversized rejected: True
history valid: True
PASS all checks matched the configured mode (clamp on, redact on).
```

**实测 2（`--max-result-chars 1000000`，夹具 600 KB < 1 MB，封顶确实不触发）**：`stored length: 600041`、`contains truncated marker: False`，其余为 True。这证明脚本确实覆盖了封顶路径，而不是只跑一个恒为真的断言。

**实测 3（`--no-redact`，运行期把 `corecoder.agent._redact` 替换为恒等函数）**：`raw secret markers in history: 4/4`、`redacted marker present: False`，其余为 True。这证明脱敏阶段确实在检查 `_redact`。

退出码：0 全部判定通过；1 任一判定失败；2 缺 key 或模型没有实际读取目标文件（脚本对模型噪声最多重试 3 次，仍失败则退出 2；换模型或重跑即可）。脚本在系统临时目录创建夹具并保留，输出中打印了 `rm -rf` 清理命令。

## 7. 追踪矩阵

| 编号 | 代码位置 | 改动点 | 自动化测试 | 人工验证 |
|---|---|---|---|---|
| M1（已实施） | 实施前 `agent.py:92-119`；现状 `agent.py:113-124`（回填函数 `299-313`） | 已实施：`try/finally` 无条件回填，幂等，覆盖 `on_tool` 回调崩溃、`ask` 回调崩溃、工具抛 `BaseException` 三类逃逸点 | 组 10（5 条，`tests/test_agent_loop_hardening.py` 类定义于 448 行）；既有 Ctrl+C 用例 `test_safety_matrix.py:171-208` | 附录 A.1 探针；`examples/m1_exception_backfill.py`（真模型实测） |
| M2 | `llm.py:20-33,209-277`；`agent.py:70-86` | 新增 `finish_reason` 字段与截断分支 | 5 条 truncat 用例 | 附录 A.2 探针 |
| R1 | `llm.py:270-292`；`agent.py:159-161,180-196,225-234,263-265` | 坏参数三处短路，先于 hooks 与权限 | 组 6（4 条） | 无独立探针，直接跑组 6（见 4.f.1） |
| R2 | `agent.py:25-29,95-101,126-133`；`cli.py:159-162,334-336` | 终止信号改为抛 `TruncatedOutput`，三个调用方各自接线（含 R4、R5） | 组 3、7、8、9（6 条） | 无独立探针，直接跑组 7（见 4.f.2） |
| M3（已实施） | 实施前 `llm.py:112-117,180-190`、`context.py:56-76,142-157`、`agent.py:73,76,122`；现状 `llm.py:108-120`、`context.py:56-86,152`、`agent.py:78-82,84-107,149` | 已实施：超限识别 + 强制压缩重试一次 + 固定开销计入估算 | 组 11（6 条，类定义于 `tests/test_agent_loop_hardening.py:590`） | 附录 A.3 探针；`examples/m3_context_overflow.py`（真实 provider，见 5.e） |
| M4（已实施） | 实施前 `agent.py:153-168`、`read.py:34-53`；现状 `agent.py:43,49,253-269,371-383`、`read.py:8,41-48` | 已实施：`_exec_tool` 出口统一封顶 + 脱敏；`read_file` 文件大小上限 | 组 12（6 条，类定义于 `tests/test_agent_loop_hardening.py:717`） | 附录 A.4 探针；`examples/m4_tool_result_governance.py`（真实 provider，见 6.e） |

## 8. 实施顺序、DoD 与回滚

三步，按“止血 → 防线 → 治理”推进。每步独立提交，失败可单独 revert。**三步均已实施完成：M1（v1.2）、M2（v1.1）、M3（v1.3）、M4（v1.4）。**

### 第一步：M1（P0，止血）——已完成

- 状态：已实施并提交（`d05e853 fix(agent): always backfill tool replies on any exception`）。
- 改动文件：`corecoder/agent.py`（`113-124`）、`tests/test_agent_loop_hardening.py`（组 10）。
- 依赖：无。
- 内容：`try/finally` 无条件回填；删除只捕获 `KeyboardInterrupt` 的分支。回填仍由幂等的 `_answer_pending_tool_calls` 完成，成功轮次是 no-op。
- 为什么先做：最小爆炸半径；它保护后续所有改动——M2 的截断分支、M3 的重试路径都可能抛异常，先保证任何异常都不撕裂历史。
- DoD（v1.2 实测）：
  - [x] `tests/test_agent_loop_hardening.py` 的 5 条 M1 用例通过（`5 passed, 15 deselected`）。
  - [x] 全量 `pytest -q` 通过：`245 passed, 1 skipped`（既有 `test_safety_matrix.py`、`test_core.py` 无回归）。
  - [x] 附录 A.1 探针输出 `history valid: True`（完整输出见 3.e）。
  - [x] `python -m corecoder --demo` 正常完成（退出码 0）。
  - [x] `examples/m1_exception_backfill.py` 真模型实测：`history valid: True`，第二轮返回 `'OK'`。
  - [x] `ruff check corecoder tests`：`All checks passed!`。
- 回滚点：`git checkout -- corecoder/agent.py tests/test_agent_loop_hardening.py`。

### 第二步：M2（P0，识别截断）——已完成（v1.1）

- 改动文件：`corecoder/llm.py`、`corecoder/agent.py`
- 依赖：M1 的 finally 不变量（截断轮回填后历史必须合法）。
- 内容：`LLMResponse.finish_reason` + 截断分支 + 连续两次终止；R1/R2 的补充修复见 4.f。
- DoD（v1.1 实测）：
  - [x] 5 条 M2 用例通过；零参数工具回归用例通过。
  - [x] 全量 `pytest -q` 通过（v1.1 为 `242 passed / 1 skipped`；v1.2 加入 M1 后为 245）。
  - [x] 附录 A.2 探针输出 `tool executed: 0`。
- 回滚点：revert 上述两个文件（R1/R2 的改动见 4.f）。

### 第三步：M3，然后 M4（P1，防线与治理）

先 M3 再 M4，理由：M3 只影响失败路径；M4 会改变每一条工具结果的文本，放在最后可以把回归面控制在一处。

**M3——已完成（v1.3）**

- 改动文件：`corecoder/context.py`、`corecoder/llm.py`、`corecoder/agent.py`、`tests/test_agent_loop_hardening.py`（组 11）、`examples/m3_context_overflow.py`
- 依赖：M1（重试失败时仍能回填）；与 M2 无直接依赖，但同一函数区域，建议按序。
- 内容：`is_context_length_error` + `fixed_tokens` + `force_collapse` + 请求级重试一次；详见 5.c。
- DoD（v1.3 实测）：
  - [x] 组 11 的 6 条 M3 用例通过（`6 passed, 20 deselected`，`-k "context or fixed_overhead"`）。
  - [x] 全量 `pytest -q` 通过：`251 passed, 1 skipped`（既有用例无回归）。
  - [x] `ruff check corecoder tests`：`All checks passed!`。
  - [x] 附录 A.3 探针输出 `chat calls: 2`、`collapse calls: 1`。
  - [x] `examples/m3_context_overflow.py --inject-400`：`collapse calls: 1`、`chat calls: 2`、`answer: 'OK...'`、`history valid: True`（真实 provider 未拒绝 1.45M token 输入，故恢复路径用注入触发，见 5.e）。
- 提交：`fix(context): recover from context-length rejection`（代码、组 11、脚本与本文件 v1.3 在同一提交）。
- 回滚点：`git revert <M3 commit>`（不改 `.gitignore`）。

**M4——已完成（v1.4）**

- 改动文件：`corecoder/agent.py`、`corecoder/tools/read.py`、`tests/test_agent_loop_hardening.py`（组 12）、`examples/m4_tool_result_governance.py`
- 依赖：无（但放在最后，避免“每条工具结果都变文本”的改动干扰 M3 的失败路径验证）。
- 内容：`_exec_tool` 出口统一封顶与脱敏 + `read_file` 文件大小上限；详见 6.c。
- DoD（v1.4 实测）：
  - [x] 组 12 的 6 条 M4 用例通过（`6 passed, 26 deselected`，`-k "clamp or redact or oversized"`）。
  - [x] 全量 `pytest -q` 通过：`257 passed, 1 skipped`（既有用例无回归）。
  - [x] `ruff check corecoder tests examples/m4_tool_result_governance.py`：`All checks passed!`。
  - [x] 附录 A.4 探针全部为 True。
  - [x] `python -m corecoder --demo` 退出码 0。
  - [x] `examples/m4_tool_result_governance.py` 默认模式：`stored length: 15076`、`contains truncated marker: True`、`raw secret markers in history: 0/4`、`redacted marker present: True`、`oversized rejected: True`、`history valid: True`；`--max-result-chars 1000000` 与 `--no-redact` 两个注入模式也各自按预期翻转（见 6.e）。
- 提交：`fix(agent): clamp and redact tool results`（代码、组 12、脚本与本文件 v1.4 在同一提交）。
- 回滚点：`git revert <M4 commit>`（不改 `.gitignore`）。

### 提交建议

```text
fix(agent): always backfill tool replies on any exception   # M1 已完成 ✔ d05e853
fix(llm): detect truncated tool calls via finish_reason    # M2（含 R1/R2）
fix(context): recover from context-length rejection        # M3 已完成 ✔
fix(agent): clamp and redact tool results                  # M4 已完成 ✔
```

顺序提交，便于逐项回滚与二分定位。

## 9. 本轮取舍

以下问题不在 4 项必须清单内，v1.1 追加的条目带 R 编号与复核严重度（P2/P3，见 0.5）。**v1.5 状态**：可观测性、Ctrl+C 取消、R3 已实施；R8 已在 `82f2c7f` 修复并核实；R6、R7 与工具实例共享、LLM client 超时、估算精度、post-hook 原文等仍按原决定不做（R5 已随 R2 一并修掉，见 4.f.2）。

| 问题 | 现状与位置 | 为什么不处理 | 残留风险 |
|---|---|---|---|
| `max_rounds` 触顶体验（R5 已部分修复） | **v1.1**：截断轮触顶改为抛 `TruncatedOutput`（`agent.py:126-133`）；纯触顶（非截断）仍返回裸字符串 `agent.py:134`；子 agent 固定 20 轮 `tools/agent.py:50` | 纯触顶属体验与预算议题，不影响正确性 | 用户可能把“纯触顶”当成正常完成；裸字符串返回值仍会被上层丢弃，嵌入方需要自己判断 |
| 可观测性（已实施，v1.5） | `corecoder/audit.py`（原名 `observability.py`，其后重命名）：审计事件（`tool_decision`/`tool_result`/`llm_call`/`run_summary`/`backfill`/`batch_interrupted`）、`usage_reported` 与成本差额；`corecoder/tracing.py`：`Tracer` 接口、`NullTracer`、JSONL、Langfuse v3 适配器。审计与 trace 默认关闭，分别由 `CORECODER_AUDIT`、`CORECODER_TRACE` 打开 | 已按“元数据默认、内容显式开启”落地；`CORECODER_TRACE_CONTENT=1` 才捕获输入输出 | trace 的脱敏只有四类模式，不能当作合规边界；`CORECODER_TRACE=langfuse` 未对真实 SaaS 做端到端验证 |
| 线程池超时与取消（取消已实施，v1.5） | 取消：`_exec_tools_parallel` 改用显式池，`shutdown(wait=False, cancel_futures=True)`，等待改为 0.1s 轮询以便及时投递 Ctrl+C，逃逸时记 `event=batch_interrupted`。工具自身的期限仍未实现 | 线程无法被杀，做“超时”只能停止等待、不能停止副作用；业界通常把期限放在工具层或 opt-in | 已运行的工具可能继续写工作区（CLI 已提示）；不自带超时的 MCP 工具仍可挂住，只是不再阻塞 Ctrl+C |
| LLM client 超时 | `llm.py:130` 未显式设置 | 属“建议优化”；SDK 有默认值 | 流式卡死时依赖 SDK 默认行为 |
| 工具实例共享 | `ALL_TOOLS` 单例；`_parent_agent` 写回 `agent.py:47-49` | 单会话 CLI 不受影响 | 同进程多 Agent 或多会话会串状态 |
| 估算精度 | `context.py:28-45` 启发式 | M3 的 fixed_tokens 缓解漏算，但估算仍是近似 | 阈值附近可能抖动；极端内容仍可能低估 |
| 并行 `on_tool` 语义（含 R6，P3） | 单工具 fire 一次 `agent.py:155-156`；并行先 fire 全部 `agent.py:256-258`。**v1.1**：`on_tool` 仍早于坏参数判定，被拒调用也会在 CLI 打印成一行 `> tool()`（`cli.py:315-316`） | 仅影响 UI 展示 | 用户可能看到“即将执行”但随后被拒绝的调用；把 `on_tool` 挪到 `_bad_arguments` 之后、或打印成 `<invalid args>` 会更诚实 |
| LLM 失败后未应答的 user 消息 | `agent.py:72` 已写入，失败后保留 | API 可接受连续 user 消息，不导致 400 | 历史语义略怪；重试时可能重复上下文 |
| post-hook 看到治理后文本 | M4 的取舍 | 安全优先，避免密钥流向 hook | 需要原文审计的场景需等 M4-full 的 `ToolResult.raw_text` |
| `raw_arguments` 无下游消费者（R3，已实施，v1.5） | 拒绝文案改为 head+tail：≤200 字符整段回显，超长时保留首 120 + 尾 80 并标注 `(N chars total)`；闸门仍是 `arguments_error`，`raw_arguments` 只用于显示（合法调用也带原文，不能据此拒绝）；`llm.py` 注释改为“仅内存保留，供拒绝文案与可选 trace”；trace 的坏参数 tool span 不写 output，捕获历史用占位符替代拒绝文案 | 已按修法建议落地 | 拒绝文案仍会把原文回显给模型（模型自己生成的，未新增泄露面）；审计只记元数据，trace 默认为空 |
| pre-hook 看不到被拒调用（R7，P3） | `_bad_arguments` 在 `_pre_hooks` 之前短路（`agent.py:159-161,263-265`），被拒调用不触发任何 hook，也不弹权限确认 | 短路是有意设计，已被 `test_malformed_arguments_do_not_reach_hooks_or_permission` 钉住；改回会让用户批准一次参数已知损坏的调用（见 4.f.1） | 把 PreToolUse hook 当审计日志用的安装，这类调用将完全不可见；另外 plan mode 下坏参数调用拿到的是“请重发”文案而非“plan mode 只读”，模型可能多试一次。两者都只影响噪声与审计，不影响安全 |
| `examples/m2` 判定逻辑失效（R8，已修复，`82f2c7f`） | phase 1 已改为“修复后应观察到 X”的断言，不再依赖坏调用被执行 | 已核实：离线运行不再因旧判定落到 `return 2` | 无（仅示例文档老化，不影响引擎） |

## 10. 待核实事项与核实命令

以下内容无法仅凭源码确定，实施前请逐条核实。

### 10.1 测试命令

- 主命令（IDE 已确认解释器路径存在）：`.venv\Scripts\python.exe -m pytest -q`
- `uv` 备选：`uv run --extra dev pytest -q`
- 核实是否可用：

```bat
uv --version
uv sync --extra dev
uv run pytest -q
```

若 `uv` 未安装或 dev extra 未同步，使用主命令。

### 10.2 行号与测试缺口复核

```bat
git grep -n "def chat" corecoder/agent.py
git grep -n "except KeyboardInterrupt" corecoder/agent.py
git grep -n "finish_reason" corecoder tests
git grep -n "arguments_error" corecoder tests
git grep -n "TruncatedOutput" corecoder tests
git grep -n "context_length" corecoder tests
git grep -n "max_rounds" tests
```

**v1.5 实测预期**（`git grep -c` 逐文件）：

| 关键词 | 预期 | 说明 |
|---|---|---|
| `finish_reason` | `corecoder/llm.py` 6 处、`corecoder/agent.py` 3 处、`tests/test_agent_loop_hardening.py` 36 处（另 `test_audit.py` 4、`test_tracing.py` 1） | 已实现（M2） |
| `arguments_error` | `corecoder/llm.py` 2 处、`corecoder/agent.py` 1 处、测试 4 处 | 已实现（R1）；R3 只改拒绝文案，闸门仍是这一处 |
| `TruncatedOutput` | `corecoder/agent.py` 4 处、`corecoder/cli.py` 3 处、`corecoder/__init__.py` 2 处、测试 8 处 | 已实现（R2）；三处接线缺一处即静默失败回归 |
| `context_length` | `corecoder/agent.py` 2 处、`corecoder/llm.py` 2 处 | 已实现（M3，v1.3） |
| `force_collapse` | `corecoder/context.py` 1 处、`corecoder/agent.py` 1 处、测试 1 处 | M3 新增的公共强制压缩入口 |
| `fixed_tokens` | `corecoder/context.py` 9 处、`corecoder/agent.py` 2 处、测试 2 处 | M3 的固定开销估算；若 agent 侧为 0，说明未计入固定开销 |
| `max_tool_result_chars` | `corecoder/agent.py` 3 处 | **已实现（M4，v1.4）**：构造参数、实例属性、封顶判断 |
| `_govern_result` | `corecoder/agent.py` 2 处 | **M4 的唯一治理出口**；若 `_exec_tool` 不调用它，封顶与脱敏都不会发生 |
| `_redact` | `corecoder/agent.py` 3 处（含 `_REDACTIONS` 定义行与 trace 输入）、测试 2 处 | M4 脱敏；测试命中处是 `--no-redact` 的 patch 目标与 trace 捕获 |
| `_MAX_FILE_BYTES` | `corecoder/tools/read.py` 3 处、测试 1 处 | M4 的 `read_file` 大小上限 |
| `max_rounds` | `corecoder/agent.py` 7 处、`corecoder/tools/agent.py` 1 处、测试 13 处 | **注意：`git grep` 只搜已跟踪文件**；测试分散在 hardening / live_smoke / audit（原 observability）/ tracing / interrupt / bad_argument 六个文件 |
| `except KeyboardInterrupt` | `corecoder/agent.py` 1 处、`corecoder/cli.py` 2 处 | **回填已不靠它**：agent 侧那一处只给 `run_summary` 标 `outcome=interrupted`；回填仍是 `try/finally`。CLI 两处是用户可见的 Interrupted 分支 |
| `batch_interrupted` | `corecoder/agent.py` 2 处、测试 4 处 | v1.5 的并行批次中断事件；若 agent 侧为 0，说明 Ctrl+C 又变回阻塞 |

v1.0 在这里的预期是“`finish_reason` 与 `context_length` 在 `corecoder` 下为 0 命中；`max_rounds` 在离线测试中为 0 命中”。M2、R1/R2、M3、M4 与 v1.5 的三项实施后，表中所有“实现类”关键词都不再是 0。

### 10.3 provider 的 context-length 文案

不同 provider 的 400 文案可能不同。核实方法：用真实 key 构造一个明显超限的请求，捕获异常并打印 `str(exc)`（临时脚本放 `%TEMP%`，不要提交）。若文案不在 `is_context_length_error` 的匹配列表中，追加该文案并补一条用例。

### 10.4 MCP 空参数行为

用一个只读 MCP 工具验证：在 `finish_reason="length"` 下，M2 修复前应观察到远端被以空参数调用；修复后应完全不调用。若手头没有 MCP 服务，可用 fake `MCPTool` 在单测中验证。

### 10.5 文档行号漂移

本文所有行号对应写作时的工作区。实施任意一项后，后续条目的行号可能偏移；每步开始前用 10.2 的命令重新定位，不要盲信旧行号。

**v1.1**：4.f 节里的行号是 M2 与 R1/R2 实施完成后实测的（`agent.py` 320 行、`llm.py` 378 行、`cli.py` 400 行、`tests/test_agent_loop_hardening.py` 429 行）。

**v1.2**：M1 实施后重新实测为 `agent.py` 322 行、`llm.py` 378 行、`cli.py` 400 行、`tests/test_agent_loop_hardening.py` 550 行。第 3 节的行号已按本次实测更新（3.a 的触发条件表与真实执行路径仍保留 v1.0 值，那是对问题现状的存档）；第 5、6 节以及 4.a–4.e 的行号仍是 v1.0 的写作时值，实施前必须重新定位。

**v1.3**：M3 实施后重新实测为 `agent.py` 345 行、`context.py` 230 行、`llm.py` 392 行、`cli.py` 400 行、`tests/test_agent_loop_hardening.py` 681 行。第 5 节与 4.f.2 的组表已按本次实测更新；第 6 节（M4）与 4.a–4.e 的行号仍是 v1.0 的写作时值，实施 M4 前必须按 10.2 重新定位。

**v1.4**：M4 实施后重新实测为 `agent.py` 383 行、`context.py` 230 行、`llm.py` 392 行、`cli.py` 400 行、`tools/read.py` 64 行、`tests/test_agent_loop_hardening.py` 824 行。第 6 节、第 7 节与 4.f.2 的组表已按本次实测更新；3.a 与 4.a–4.e 的修复前存档行号保持不变。

**v1.5**：第 9 节残留项落地后重新实测为 `agent.py` 794 行、`context.py` 242 行、`llm.py` 420 行、`cli.py` 410 行、`tools/read.py` 64 行、`observability.py` 258 行、`tracing.py` 424 行、`tests/test_agent_loop_hardening.py` 824 行；全量 `pytest -q` 为 **322 passed / 1 skipped**（31.70s），`ruff check corecoder tests` 通过（venv 内 0.17 与 PATH 0.15 均通过）。真实 provider 脚本：`m5_observability_audit.py` 默认/注入/降级三段通过，`m6_interrupt_cancel.py` 默认/`--signal`/`--blocking-shutdown` 通过，`m7_bad_argument_diagnostics.py` 默认/`--head-only` 通过。第 2–6 节的行号仍是各自版本实施时的实测值，继续改动前必须按 10.2 重新定位。

## 附录 A：人工验证探针

所有探针只依赖仓库内代码，不需要 API key。放到 `%TEMP%` 下运行，避免修改仓库。

### A.1 M1：异常后回填（v1.2 已实测）

```python
"""M1 probe: an exception in on_tool must not leave dangling tool_calls."""
from typing import ClassVar

from corecoder import Agent
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.tools.base import Tool


class Echo(Tool):
    name = "echo"
    description = "echo"
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def execute(self, text: str) -> str:
        return text


def boom(*_args, **_kwargs):
    raise RuntimeError("ui boom")


script = [
    LLMResponse(tool_calls=[ToolCall(id="t1", name="echo", arguments={"text": "hi"})]),
    LLMResponse(content="done"),
]
agent = Agent(llm=ScriptedLLM(script), tools=[Echo()])

try:
    agent.chat("go", on_tool=boom)
except RuntimeError as exc:
    print(f"raised: {exc.__class__.__name__}")

replies = [m["tool_call_id"] for m in agent.messages if m.get("role") == "tool"]
last = next((m["content"] for m in agent.messages if m.get("role") == "tool"), None)
ids = [tc["id"] for m in agent.messages if m.get("role") == "assistant"
       for tc in (m.get("tool_calls") or [])]
print(f"tool replies: {replies}")
print(f"last tool content: {last}")
print(f"history valid: {sorted(ids) == sorted(replies)}")
```

### A.2 M2：截断轮不执行

```python
"""M2 probe: a truncated tool call must not be executed."""
from typing import ClassVar

from corecoder import Agent
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.tools.base import Tool


class Spy(Tool):
    name = "spy"
    description = "records execution"
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}
    calls = 0

    def execute(self) -> str:
        type(self).calls += 1
        return "executed"


Spy.calls = 0
script = [
    LLMResponse(finish_reason="length", tool_calls=[ToolCall(id="t1", name="spy", arguments={})]),
    LLMResponse(content="done"),
]
agent = Agent(llm=ScriptedLLM(script), tools=[Spy()])
answer = agent.chat("go")
reply = next(m["content"] for m in agent.messages if m.get("role") == "tool")
print(f"tool executed: {Spy.calls}")
print(f"tool reply: {reply}")
print(f"answer: {answer}")
```

### A.3 M3：超限后压缩重试

```python
"""M3 probe: context-length 400 -> one forced collapse -> one retry."""
from unittest import mock

from openai import BadRequestError

from corecoder import Agent
from corecoder.llm import LLMResponse


def bad_request(message):
    try:
        import httpx
    except ModuleNotFoundError:
        import httpx2 as httpx
    req = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    return BadRequestError(message, response=httpx.Response(400, request=req), body=None)


class FakeLLM:
    model = "fake"
    total_prompt_tokens = 0
    total_completion_tokens = 0
    estimated_cost = None

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
        self.calls += 1
        if self.calls == 1:
            raise bad_request("This model's maximum context length is 8192 tokens")
        return LLMResponse(content="recovered")


llm = FakeLLM()
agent = Agent(llm=llm, tools=[])
agent.messages = [{"role": "user", "content": f"m{i}"} for i in range(6)]
agent.context._get_summary = lambda messages, model=None: "summary"

with mock.patch.object(agent.context, "force_collapse",
                       wraps=agent.context.force_collapse) as spy:
    answer = agent.chat("go")

print(f"chat calls: {llm.calls}")
print(f"collapse calls: {spy.call_count}")
print(f"answer: {answer}")
```

### A.4 M4：封顶与脱敏

```python
"""M4 probe: oversized tool output is clamped and secrets are redacted."""
from typing import ClassVar

from corecoder import Agent
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.tools.base import Tool


class Big(Tool):
    name = "big"
    description = "returns a large payload"
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}

    def execute(self) -> str:
        return "HEAD\n" + "sk-" + "a" * 32 + ("x" * 100_000) + "\nTAIL"


script = [
    LLMResponse(tool_calls=[ToolCall(id="t1", name="big", arguments={})]),
    LLMResponse(content="done"),
]
agent = Agent(llm=ScriptedLLM(script), tools=[Big()], max_tool_result_chars=15000)
agent.chat("go")
stored = next(m["content"] for m in agent.messages if m.get("role") == "tool")

print(f"stored length: {len(stored)}")
print(f"contains truncated marker: {'truncated:' in stored}")
print(f"raw key present: {'sk-' + 'a' * 32 in stored}")
print(f"redacted marker present: {'[REDACTED' in stored}")
print(f"head preserved: {stored.startswith('HEAD')}")
print(f"tail preserved: {stored.endswith('TAIL')}")
```

