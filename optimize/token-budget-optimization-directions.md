> 只读方向备忘 · 不改动任何源码或配置。
> 依据来自同目录 [token-budget-industry-findings.md](./token-budget-industry-findings.md) §1–§3 的检索事实；本文只给"改什么 / 为什么改（钉哪家事实）/ 预期消除哪类撞墙 / 优先级 / 改动范围与风险"，不写具体补丁。
> 行号为本轮逐条实读核对后的当前行（抽检 20 处全部吻合）。文末 §六 记录与上一版的差异：三处硬伤、三处漏项、一处顺序调整。

# CoreCoder token 预算 · 可执行优化方向（按优先级）

## 一、核心矛盾（大白话）

业界这五家把一次请求看成三件事分别管：**"模型真实窗口 `W_in`" 和 "我自己想提前压缩的工作点阈值" 是两个数**（goose 运行期解析、continue `32_768` 只是兜底）；**"本次还要生成多少输出"要在压缩预算里占一头**（goose 宁可停在 272k 价格档之下、明确"不是最大请求尺寸"）；**估算宁可高不可低**（SWE-agent/goose 真分词，continue 近似也 ×1.18~1.26）。

本项目现状把这三件事压成了**一个标量** `max_context_tokens`，默认 `128_000`（`config.py:33`），可由 `CORECODER_MAX_CONTEXT` 覆盖（`config.py:53`）——它同时充当"模型真实窗口"和"客户端压缩阈值"；判定项是 `estimate_tokens(messages) + fixed_tokens`（`context.py:66`，已计入 system/schema 等固定开销），**不含"本次还要生成多少"**；估算是无系数的字符近似（`context.py:30-37`）。于是核心矛盾是：

> **压缩阈值描述的"重量"，不等于真正发给服务商的那次请求的重量**——因为没算上"本次输出额度"、没算上近似误差、也没跟"模型真实窗口"对齐。只要这三处任意一处偏差叠加，`0.90` 看着安全、请求仍会越界，只能靠事后 `_recover_overflow` 兜底（`agent.py:390-408`），而它按现有契约**只折一次**——这条契约同时被测试（`test_agent_loop_hardening.py:635-636`）和 example（`examples/m3_context_overflow.py:247-250`）两端钉死。

下面每条方向都指向"让阈值重新描述真实请求"这一主线。

---

## 二、优化方向清单

### OPT-1 · P0 · 把"模型真实窗口"从"客户端压缩阈值"里拆出来

- **改什么**：`config.py:33/:53` —— 让"模型真实输入窗口 `W_in`"成为独立、可按模型配置/查表的一等概念；客户端压缩工作点 `max_context_tokens` 由 `W_in` 派生，而非现在这样两者混成同一个 `128_000`。
- **窗口表怎么做（本版修正）**：**内置表 + 用户覆盖，不接 litellm**。照搬 `llm.py:68-111` 已经在用的那套形状——内置 `_WINDOWS`（model → W_in）+ `WINDOWS_FILE = Path.home() / ".corecoder" / "windows.json"` 同结构覆盖 + 文件缺失/损坏时静默回退内置（`_load_pricing` 的 `except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError)` 同一写法，`llm.py:101-111`）。理由：litellm 是 **optional extra**（`pyproject.toml:41`），核心依赖只有 openai/rich/prompt_toolkit/python-dotenv 四项（`pyproject.toml:30-35`）；走 litellm 查表会引入"没装 extras 就整条路径失效"的分支，也与项目"约 3000 行最小实现"的定位相悖。内置表 + json 覆盖零新依赖、离线可用，且用户换新模型不用等发版——这正是 `pricing.json` 存在的理由（`.env.example:150-151` 已把它文档化）。
- **为什么改（依据）**：goose 窗口**不写死**、运行期 `provider.get_context_limit(model, override)` 解析（`context_limit.rs`）；continue `DEFAULT_CONTEXT_LENGTH = 32_768`（`constants.ts:2`）只是**模型没自带 contextLength 时的兜底**；SWE-agent/aider 从 `litellm.model_cost` / `model_prices_and_context_window.json` 按模型查（`models.py:599-607`、`models.py:162-165`）。→ 共识：**真窗口是模型属性，阈值是客户端策略，不能同值**。
- **预期消除的撞墙**：当切到真实窗口小于 128000 的模型（dogfood 换 deepseek/kimi 的现实场景）时，`0.90×128000≈115200` 远超模型真窗 → 每轮大请求被 provider 判 400、`_recover_overflow` 只折一次仍可能失败。拆分后阈值天然贴合真窗。
- **改动范围（本版补全，共 6 处）**：
  1. `config.py`：新增 `window_tokens`（或 `model_window`）字段 + `from_env` 默认值来源（`CORECODER_MAX_CONTEXT` 语义改为"阈值"，新增 `CORECODER_MODEL_WINDOW`；两者都不设时 `W_in=128000`、阈值派生，保证旧 `.env` 行为不变）。
  2. `context.py`：构造入参语义（`ContextManager` 只该拿到派生后的阈值，不该再兼任"窗口"）。
  3. `agent.py:69/:100`：构造参数与 `ContextManager(max_tokens=...)` 传参。
  4. **`corecoder/tools/agent.py:49`**：子 agent 继承 `parent.context.max_tokens`。只拆父级字段会留下"父用真窗、子用阈值"的半拆状态，必须改为继承同一个派生值（或同时继承 `W_in` 与阈值两者，并明确子 agent 用哪个）。
  5. **`corecoder/cli.py:99`**：命令行构造 `Agent(max_context_tokens=config.max_context_tokens)` 的入口，字段改名/拆分后这里是最容易漏的一处。
  6. **`.env.example:41-42`**：注释已写明"上下文窗口预算（token）。默认 128000（config.py::Config.from_env）"，语义拆分后必须同步；若同时做 OPT-6，`.env.example:34-36` 的 `CORECODER_MAX_TOKENS` 说明也要一并对齐。
- **联动（易被忽略）**：`agent.py:174-177` 用 `self.context.max_tokens * pct / 100` 作为 MCP 工具延迟加载的体积门限，而 `.env.example:136-138` 已把它文档化为"MCP schema 估算 token 占上下文窗口（CORECODER_MAX_CONTEXT）的百分比"。字段一拆，这个"窗口"基准必须明确指定是 `W_in` 还是压缩阈值，否则 `CORECODER_TOOL_SEARCH` 的触发点会随拆分漂移。
- **风险**：**中低（本版下调）**。不引网络、不引新依赖是主要收益；残留风险是旧 `.env` 兼容（`CORECODER_MAX_CONTEXT` 语义从"窗口"变"阈值"）与父子 agent 一致性。README 未提 `max_context`，无需同步。

### OPT-2 · P1 · 压缩预算显式纳入"本次输出额度"（必须带 clamp）

- **改什么**：`context.py:66` 的 `current = estimate_tokens(messages) + fixed_tokens` —— 判定项再加一项输出预留 `output_reserve`；相应地 `agent.py:310-311/333-334` 在调用 `maybe_compress` 时把输出额度一并传入。
- **clamp 约束（本版新增，硬要求）**：不能把 `max_tokens` 原样加进去。至少写成
  `output_reserve = min(max_tokens, max(0, W_in - fixed_tokens))`，
  并建议再叠一个比例上限（如不超过 `W_in` 的 50%）。原因：小窗口配置下（例如 `CORECODER_MAX_CONTEXT=2000` 配默认 `max_tokens=4096`）原样相加会让**每一次**请求都越过 `0.50/0.70/0.90` 三档，反复触发 collapse——`context.py:86-90` 只有 `len(messages) > 4` 的保护，不防抖。另有边界：若 `W_in - fixed_tokens <= 0`（system + schema 已吃满窗口），压历史压不动，应显式告警而不是空转。
- **正确性依赖 OPT-1**：clamp 的分母必须是 `W_in`。OPT-2 可以先落地，但在 OPT-1 之前只能拿 `max_context_tokens` 当基准，属于过渡取值——文档层面要写明"此版 clamp 是近似"，避免后续误以为已经对齐真窗。
- **输出额度从哪里读（本版补充）**：`Agent` 构造器没有 `max_tokens` 字段（`agent.py:65-77`），当前额度只存在于 `llm.extra`（`cli.py:87` 构造时传入，`examples/m2_truncated_tool_call.py:180` 还会运行期改）。从 `llm.extra` 读比新增一个 Agent 参数更贴合现有用法，也不会与 OPT-6 的"按模型配"打架。
- **为什么改（依据）**：本次核到**没有一家在压缩阈值里减输出额度**（SWE-agent `models.py:701-703` 只比 `input_tokens`、goose `mod.rs:283-289` 只用 `current_tokens`）——这是**全行业共同的坑**；goose 用"停在 272k 价格档之下、This is not a maximum request size"（`mod.rs:23-25`）示范了"主动留头寸而非顶满"。本项目主动补上即为**优于现状**。
- **预期消除的撞墙**：`0.90` 阈值下"输入+fixed 恰好通过、加上本次输出后越界"导致的 provider 400，以及因输出额度被挤占而频繁触发的 `finish_reason == "length"` 截断。
- **改动范围**：`context.py`（判定公式 + clamp）+ `agent.py`（传参）。纯判定改动，不动数据结构。
- **风险**：**低-中（本版上调）**。带 clamp 后不会出现小窗口恒定触发；但"压缩更早"仍会带来"过早 summarization/collapse 丢信息"的代价，需评估并扩展 `fixed_overhead` 相关用例（见 §四）。

### OPT-3 · P1 · token 估算加对外安全系数，或引入真实分词器

- **改什么**：`context.py:30-37` `_approx_tokens`（当前 CJK/1.5、符号密集/2.8、其余/3.4 的纯字符近似、无对外系数）—— 叠加一个可配置安全乘子；进一步可换成真实 tokenizer 计数。
- **落地位置（本版新增，必须遵守）**：系数**不要写进 `_approx_tokens` 内部**。`tests/test_core.py:73-76` 对三档系数做了 `pytest.approx(len/1.5, rel=0.1)` / `(len/3.4)` / `(len/2.8)` 的精确断言，在函数内直接乘系数会当场打红这三条。正确做法是让 `_approx_tokens` 保持"纯三档"语义，系数加在 `estimate_tokens` 外层（或给 `_approx_tokens` 加默认 1.0 的形参，调用方传 Config 里的值）。系数从 `Config` 字段读，**不要**在函数体内 `os.getenv`，避免测试间环境变量污染（本仓库已踩过此类坑）。
- **为什么改（依据）**：SWE-agent/goose 用真分词器（`models.py:690-694`；`mod.rs:266-281` 有 `session.usage.total_tokens` 用真实用量否则 `create_token_counter()`）；continue 即便用近似也明确 `ANTHROPIC×1.23 / GEMINI×1.18 / MISTRAL×1.26` 且 `Math.ceil`（`getAdjustedTokenCount.ts`），chunk 尺寸还 `512 - buffer for safety`（`constants.ts:14`）。`context.py` docstring 自己承认"flat 3-chars 把中文会话读成实际一半大小→压缩触发太晚"。→ 共识：**估算宁可高估**。
- **联动回归（本版新增，硬要求）**：`estimate_tokens` 不只服务压缩。`agent.py:174-177` 用它算 `schema_tokens` 并与 `self.context.max_tokens * pct / 100` 比较，决定 MCP 工具是否延迟加载；`.env.example:136-138` 已把这个门限文档化为"占上下文窗口的百分比"。因此加系数（或 OPT-1 改 `max_tokens` 语义）**会同时挪动 `CORECODER_TOOL_SEARCH` 的触发点**，必须回归 `tests/test_schema_optimization.py`（尤其 `:174`、`:334`、`:342` 对 `estimate_tokens(system)+estimate_tokens(schemas)` 的恒等断言，和 `:261` 的 env 清理），并在落地时明确该门限的"窗口"基准与 OPT-1 保持一致。
- **预期消除的撞墙**：因低估占用导致压缩触发偏晚、请求实际已越界才被动撞墙（与 OPT-2 协同：一个补"输出项"、一个修"输入估准度"）。
- **改动范围**：`context.py`（在 `estimate_tokens` 外层乘系数；`_snip_at/_summarize_at/_collapse_at` 三档阈值本身不用动，它们仍以 `max_tokens` 为基数）+ `config.py`（系数字段）+ `agent.py:174-177`（门限基准对齐）。
- **风险**：**中**。系数选值需实测校准；真 tokenizer（tiktoken）对 CJK/代码与服务商口径不一致，可能反而偏移——建议"近似 + 系数"先行（默认 1.0 保持现状，校准后再调），真分词作为可选后端。

### OPT-4 · P1 · 超窗恢复从"折一次"改为"迭代到有界上限"

- **改什么**：`agent.py:390-408` `_recover_overflow`（现：`force_collapse` 一次 → `_chat_once` 重试一次；再失败即抛）。
- **为什么改（依据）**：codex 超窗时 `history.remove_first_item(); retries = 0; continue`——**从头部裁一条后循环重试直到装得下**（`compact.rs`）；goose 报 `ContextLengthExceeded` 时**渐进移除最旧消息再重试**（`mod.rs` 测试 `test_progressive_removal_on_context_exceeded`）。
- **预期消除的撞墙**：单次 `force_collapse` 不足以压进真窗时（长会话/多轮累积）仍失败的场景；把"一次兜底"升级为"有界多次兜底"。
- **必须同批改的契约（本版补全为两处）**：
  1. `test_agent_loop_hardening.py:621-636` `test_context_overflow_retries_exactly_once` —— 当前明确断言 `collapse.call_count == 1 && llm.calls == 2` 且必须抛 `BadRequestError`；改为"有界多次"后该用例语义整体失效，须重写为"迭代到上界后仍失败才抛"。
  2. **`examples/m3_context_overflow.py:247-258`** —— PASS 判据同样写死 `collapse.call_count == 1 and state["chat_calls"] == 2`，并配 `:187` 的面板标题 "collapse once -> retry once" 与 `:257` 的 FAIL 文案。只改 pytest 不改这里，m3 会从 PASS 直接翻成 FAIL，而 m3 正是对外演示这条路径的脚本。
- **回归项（不需改，但必须跑绿）**：`test_audit.py:427-447` 断言 `context_overflow` 事件恰好 1 条——迭代实现下每个失败回合仍应只记一条；`test_agent_loop_hardening.py:650-661`（`len(messages)<=4` 不折）的边界必须保留，对应 `agent.py:370` 的保护。
- **边界（本版新增）**：本方向解决的是"**多轮累积**型超窗"。对"**单条超大**"型（`examples/m3_context_overflow.py:113-125` 那种 90 万字符 filler + 六条小消息）删历史毫无用处——要删的那条就是唯一的大块。工具产出侧已有 `max_tool_result_chars=15_000` 兜底（`agent.py:73`），用户手动粘贴的超长输入没有这层保护。落地时必须写下这条边界，否则事后会误判"迭代恢复万能"；单条截断应另立一项，不要混进 OPT-4。
- **改动范围**：`agent.py`（恢复循环 + 上界）+ 上述两处契约。
- **风险**：**中，且与两处既有契约直接冲突**。除改写用例与 example 外，还需设重试上界（建议 2-4 次或 `len(messages)//2`）防死循环与成本失控。

### OPT-5 · P2 · 压缩触发叠加"绝对头寸上限"（百分比 ∧ 绝对值取小）

- **改什么**：`context.py:54-56`（现仅 `0.50/0.70/0.90` 三个窗口百分比）—— 在百分比之外再叠一个绝对 token 上限，取二者更小，仿 goose。
- **为什么改（依据）**：goose `DEFAULT_AUTO_COMPACT_TOKEN_LIMIT = 225_000` 与 `threshold.min(token_limit/context_limit)`（`mod.rs:25/41`）——**既按比例又留绝对头寸**，且停在价格档之下；aider 用 `clamp(window/16, 1024, 8192)`（`models.py:355-358`）以绝对值夹顶。
- **前提**：绝对上限的意义建立在"知道 `W_in`"之上（否则只是第二个魔法数），因此**排在 OPT-1 之后**。
- **预期消除的撞墙/代价**：大窗口模型下"0.90×大窗"仍产生跨价格档的巨额请求（成本/延迟失控），以及超大输入百分比压不动。属**成本与稳健性**收益，非直接可用性阻塞，故 P2。
- **改动范围**：`context.py` + `config.py`（新增绝对上限项，走 `from_env`，不设即不启用）。
- **风险**：**低**（对中小窗口模型行为几乎不变）。

### OPT-6 · P2 · 输出上限与模型绑定 / 推理模型单列预算

- **改什么**：`config.py:31/:51`（`max_tokens=4096` 全局写死、与 model 无关）与 `llm.py` 请求参数（`llm.py:217-228` 直接 `**self.extra` 展开，`cli.py:87` 是唯一注入口）—— 让输出上限可按模型配置；对推理模型给"思考链 + 回答"合并的更宽预算。
- **方言风险已存在兜底（本版补充，风险下调）**：`llm.py:128-145` `_adapt_rejected_param` 已在 400 时自动把 `max_tokens` 翻成 `max_completion_tokens`、并把被拒的 `temperature` 丢掉，`:245-253` 负责带新参数重试。所以本方向**不需要再写一层 provider 方言层**，只需"按模型喂对数字"。
- **为什么改（依据）**：SWE-agent 按模型查 `max_output_tokens` 并有 Claude 特例 `= 64000`（`models.py:604-615`）；continue 单列 `DEFAULT_REASONING_TOKENS = 2048`（`constants.ts:5`）。CoreCoder `llm.py:188-191` 的 reasoning 不入历史但**仍计入服务端输出**，故输出额度必须为它留够。本仓库 `.env.example:34-36` 已经记录过同类现实约束："小米 mimo 网关顶 131072，取 16384"——说明"输出上限必须与窗口联动"是真实痛点，而非纸面推理。
- **预期消除的撞墙**：换推理模型时，输出被思考链吃满导致 `finish_reason == "length"` 截断、触发 M2 guard（`agent.py:319-321`）。注意 guard 的实际行为是"第一次拒绝执行并回提示，连续两次才抛 `TruncatedOutput`"（`agent.py:433-443`），不是一次即抛。
- **改动范围**：`config.py` + `llm.py`（按模型表取默认输出额度；推理模型单独给预算）+ `.env.example:34-36`（说明同步）。
- **风险**：**低-中（本版下调）**。主要成本是"模型表又要多维护一列"，不是协议差异。

---

## 三、实施顺序（本版重排）

0. **验证前置：给 examples 加参数**（见 §四 4.1）。不做这步，OPT-2/OPT-4/OPT-6 的端到端验证全部无法执行。
1. **OPT-3 + OPT-2 同批**。两者改的是同一个判定公式的两个因子（估算准度 + 输出预占），拆开做要跑两遍回归；OPT-2 带 clamp，OPT-3 系数默认 1.0 上线（行为不变），校准后再调。
2. **OPT-1**。为 OPT-2 的 clamp、OPT-5 的绝对头寸、OPT-6 的按模型输出提供正确基准 `W_in`；同批处理 6 处依赖点与 MCP 门限基准。
3. **OPT-4**。单独一批，pytest 契约 + m3 example 判据一起改，并写下"单条超大不覆盖"边界。
4. **OPT-5 / OPT-6**。成本与模型贴合的锦上添花，都依赖 `W_in` 先落地。

> 依赖关系：OPT-5/OPT-6 的绝对/真窗语义依赖 OPT-1；OPT-2 的 clamp 分母依赖 OPT-1（在此之前用 `max_context_tokens` 过渡）；OPT-3 可独立先行，但必须带上 `agent.py:174-177` 门限回归。

---

## 四、验证方法（全部可复算）

### 4.1 前置改造：让 m2/m3 接受参数（否则端到端命令无效）

**现状陷阱**：`examples/m3_context_overflow.py:194/:208` 把 `max_context_tokens=20_000_000` 写死（原意是"假装本地预算无限"），`:84` 把 `max_tokens=16` 写死；`examples/m2_truncated_tool_call.py:151` 写死 `build_llm(max_tokens=96)`，`:180` 又自行 `llm.extra["max_tokens"] = cap` 走 `:175` 的 512→4096 阶梯。因此 `CORECODER_MAX_CONTEXT` / `CORECODER_MAX_TOKENS` **只对 CLI 生效**（`cli.py:87`、`cli.py:99`），对这两个 example 完全无效——直接照抄环境变量命令会得到"什么都没发生"的假阴性。

| 目标 | 现状硬编码点 | 改法 |
|---|---|---|
| m3 窗口 | `m3:194`、`m3:208` `max_context_tokens=20_000_000` | 加 `--max-context`，优先级 `--max-context` > `CORECODER_MAX_CONTEXT` > 脚本原值 20_000_000；演示"无限预算"时仍显式传 20000000 |
| m3 输出额度 | `m3:84` `max_tokens=16` | 加 `--max-tokens`，优先级同上（最后回落 16）。**注意**：16 是探针形状的一部分（只生成一两个 token），而当前 `.env` 已设 `CORECODER_MAX_TOKENS`——env 一旦参与默认值，m3 无参跑的请求形状就会变，改造时必须显式决定是否保留 16 |
| m2 输出额度 | `m2:151` `build_llm(max_tokens=96)` | 加 `--max-tokens`，同样三级优先级，最后回落 96 |
| m2 阶梯覆盖问题 | `m2:175` 固定 5 档、`m2:180` 每轮覆盖 | 加 `--no-ladder`（单档跑），否则命令行给的额度会被阶梯首档 512 覆盖；同时把 `--max-tokens` 说清是"单档值"还是"阶梯起点" |

> 原则：显式参数 > 环境变量 > 脚本原值。这里有一处**有意的行为变化**必须承认：当前 `.env` 未设 `CORECODER_MAX_CONTEXT`（m3 仍是 20M），但**已设 `CORECODER_MAX_TOKENS`**（m3 的输出额度会从 16 变掉）。所以"无参行为与今天完全一致"只在未设 env 时成立，不能写成无条件承诺。

### 4.2 各方向验证映射

| 方向 | 验证方式 | 钉住的用例/脚本 | 备注 |
|---|---|---|---|
| OPT-2 / OPT-3 | pytest | `test_agent_loop_hardening.py:663/677/729` | 现只断言 `fixed_tokens`，需扩展为"判定项含 output_reserve""估算含系数" |
| OPT-3 | pytest（必须回归） | `test_core.py:73-76`（三档 approx）、`test_schema_optimization.py:174/334/342/261` | 系数放外层才能保绿；门限基准要一起对齐 |
| OPT-2 / OPT-1 | 端到端（改造后） | `examples/m3_context_overflow.py --max-context 2000 --max-tokens 512` | 审计应先出现 `context_compressed`（`context.py:73/81/89` 的 layer=snip/summarize/collapse）**先于**任何 provider 400 |
| OPT-1 | pytest | `cli` 构造路径 + `tools/agent.py:49` 子 agent | 需新增"父子 agent 用同一基准"用例 |
| OPT-4 | pytest（改契约后） | `test_agent_loop_hardening.py:605/621/650`、`test_audit.py:427` | `:621` 从"exactly once"改为"有界多次"；`:650` 保留"太短历史不折" |
| OPT-4 | 端到端（改判据后） | `examples/m3_context_overflow.py --inject-400` | `:247-258` 判据改为"多次 `context_forced`（`context.py:98`）后恢复成功" |
| OPT-6 / OPT-2 | 端到端（改造后） | `examples/m2_truncated_tool_call.py --max-tokens 64 --no-ladder` | 出现 `output_truncated`（`agent.py:435`）、tool 未执行、末尾 `TruncatedOutput` 文案（`agent.py:341/441`）；用例 `test_agent_loop_hardening.py:129/154/184` |
| 回归护栏 | pytest 全量 | 见 §五 命令 | 审计事件可核对性由 `test_audit.py:406/:427` 保证 |

### 4.3 判定"成功"的可复算信号

- **输出截断被防护**：m2 运行日志中 `output_truncated` 出现且无 tool 被执行（对照 `agent.py:319-321`），pytest `-k "truncated or finish_reason or consecutive"` 全绿。
- **输入超窗被压缩**：m3（小 `--max-context`）在收到任何 400 **之前**就打出 `context_compressed`；OPT-4 落地后，真窗被越界时应出现"多次 `context_forced`（`context.py:98`）后成功恢复"，而非一次即抛。

---

## 五、回归清单（按触碰面，可直接复制执行）

```bat
.venv\Scripts\python.exe -m pytest tests/test_agent_loop_hardening.py tests/test_core.py tests/test_audit.py tests/test_schema_optimization.py -q
```

| 触碰面 | 必跑用例 | 为什么 |
|---|---|---|
| 压缩判定 / fixed 开销 | `test_agent_loop_hardening.py:663/677/687/729`、`test_core.py:83/94/109/122` | 直接构造 `ContextManager(max_tokens=1000~3000)`，新参数必须带默认值 |
| 估算函数 | `test_core.py:58/65/73-76/79` | 三档 `pytest.approx` 精确断言，是 OPT-3 的硬边界 |
| MCP 延迟加载门限 | `test_schema_optimization.py:174/261/334/342` | `estimate_tokens` 与 `max_tokens` 语义同时驱动它 |
| 超窗恢复 | `test_agent_loop_hardening.py:605/621/638/650` | 含"非超窗 400 不折"与"太短历史不折"两条负向契约 |
| 审计事件 | `test_audit.py:406/412-414`（`context_compressed` 恰好 1 条 + layer=snip）、`:427-447`（`context_overflow` 恰好 1 条） | OPT-2/OPT-4 会改事件条数与字段 |
| Example 自检 | `m2`、`m3` 改造后各跑一次无参 + 带参 | 保证"无参行为不变" |

---

## 六、本版与上一版的差异（核验记录）

| # | 上一版写法 | 本版修正 | 证据 |
|---|---|---|---|
| 1 | 上一版 §三（验证方法）用 `CORECODER_MAX_CONTEXT=2000 … m3` / `CORECODER_MAX_TOKENS=64 … m2` 做端到端 | 两条命令在当前代码上**不生效**；改为先给 examples 加 `--max-context/--max-tokens`（env 作默认值） | `m3:194/:208/:84`、`m2:151/:175/:180`、`cli.py:87/:99` |
| 2 | OPT-4 只提改 `test_agent_loop_hardening.py:621` | 补 `examples/m3_context_overflow.py:247-258`（PASS 判据）+`:187`/`:257` 文案，共两处契约 | `m3:247-258` |
| 3 | OPT-1 改动范围只列 `config.py`/`context.py`/`agent.py:100` | 补 `tools/agent.py:49`、`cli.py:99`、`.env.example:41-42`（+`agent.py:174-177` 门限基准联动） | 逐行实读核对 |
| 4 | OPT-1 窗口表提议 litellm 查表，风险"中" | 改为内置表 + `~/.corecoder/windows.json` 覆盖（照 `_load_pricing` 形状），不接 litellm，风险降为"中低" | `pyproject.toml:41`、`llm.py:68-111` |
| 5 | OPT-2 无 clamp，风险"低" | 加 `min(max_tokens, W_in - fixed)` + 比例上限，并注明正确性依赖 OPT-1 的 `W_in`；风险调为"低-中" | `context.py:86-90`、`agent.py:370` |
| 6 | OPT-3 只提"改估算函数 + 联动三档阈值" | 补"系数必须放 `estimate_tokens` 外层（否则打红 `test_core.py:73-76`）"与 MCP 门限回归 | `test_core.py:73-76`、`agent.py:174-177`、`.env.example:136-138` |
| 7 | 顺序：OPT-2 → OPT-3 → OPT-1 → OPT-4 → OPT-5/6 | 前置 examples 参数化；OPT-3+OPT-2 同批；OPT-1 → OPT-4 → OPT-5/6 | 同一公式两因子同批回归成本最低 |

---

> 以上均为只读方向；任何改动需另行确认后再动 `corecoder/` 源码或 `.env`。执行 OPT-4 前请留意它会同时修改 `test_agent_loop_hardening.py:621` 与 `examples/m3_context_overflow.py:247-258` 两处判据。
