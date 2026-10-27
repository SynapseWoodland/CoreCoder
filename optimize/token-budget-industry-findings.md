> 只读备忘 · 不改动任何源码或配置。
> 本文所有数值均来自本次对 GitHub 的实时抓取（github MCP 凭证失效，改用 `api.github.com/.../contents` 取 base64 + 本地解码）。
> 每条出处标注 `owner/repo @分支 · 文件:行 · 原文`；凡本次未检索到 / 仓库已重组 / 无源码的，均如实标注，不编数值。
> 抓取日期见 Git 历史；litellm 表与服务商模型卡会变动，涉及"某模型真实窗口"的结论需按下文方法重新核对。

# 编码 Agent 的 token 预算做法 × CoreCoder 差距备忘

## 0. 一句话结论

- 业界共性：**输出上限尽量按模型查表/留小、窗口优先用模型元数据（客户端常量只兜底）、压缩用窗口百分比再叠绝对头寸、估算要么真分词要么乘安全系数、撞墙先自救**。
- CoreCoder 触发结构（百分比 0.5/0.7/0.9）与业界同族、M2 与超窗重试已到位；**两处该补**：压缩预算未减本次输出额度、字符近似无对外安全系数。
- 落到配置：**`CORECODER_MAX_CONTEXT ≈ W_in − CORECODER_MAX_TOKENS − 余量`**（`W_in`=模型真实输入窗口，需实时查得，本文不凭记忆给具体模型的数）。

---

## 1. 检索到的事实（每条钉出处）

### 1.1 SWE-agent/SWE-agent @main — `sweagent/agent/models.py`

- **输出上限不写死，查 litellm 表、可按 config 覆盖**（`models.py:599-607`）：
  - `self.model_max_input_tokens = self.config.max_input_tokens`（若设置）`else` `litellm.model_cost.get(self.config.name, {}).get("max_input_tokens")`（:599-602）
  - `max_output_tokens` 同理走 `litellm.model_cost...get("max_output_tokens")`（:604-607）
- **Claude 特例硬编码 64000**（无 128k beta 头时）：`self.model_max_output_tokens = 64000`（`models.py:615`，位于 608-620 分支内）。
- **仅 anthropic 才把上限塞进请求**：`if self.lm_provider == "anthropic": completion_kwargs["max_tokens"] = self.model_max_output_tokens`（`models.py:712-713`）。
- **用真实分词器估算**：`litellm.utils.token_counter(messages=..., model=..., custom_tokenizer=...)`（`models.py:690-694`）。
- **压缩判定只卡输入侧、不预留本次输出额度**：`elif input_tokens > self.model_max_input_tokens > 0: raise ContextWindowExceededError`（`models.py:701-703`）。
- **超窗不自动重试**：`retry=retry_if_not_exception_type((ContextWindowExceededError, ... litellm.exceptions.ContextWindowExceededError, ...))`（`models.py:809-831`）——把超窗排除在重试白名单外，直接抛。
- （历史裁剪按条数、非 token：`sweagent/agent/history_processors.py:85-176` `LastNObservations` 保留最近 n 条 observation。）

### 1.2 Aider-AI/aider @main — `aider/models.py`

- **表来源 URL**：`MODEL_INFO_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"`（`models.py:162-165`）。
- **历史预算绝对值、夹在窗口 1/16**：
  - 默认 `self.max_chat_history_tokens = 1024`（`models.py:339`）
  - `max_input_tokens = self.info.get("max_input_tokens") or 0`；`self.max_chat_history_tokens = min(max(max_input_tokens / 16, 1024), 8192)`（`models.py:355-358`）——即 `clamp(window/16, 1024, 8192)`。
- （openrouter 兜底把 `max_input/max_tokens/max_output` 都置成 `context_size`，`models.py:313-319`。）

### 1.3 openai/codex @main — `codex-rs/core/src/compact.rs`

- **近似分词器**（非精确）：`pub use codex_utils_output_truncation::approx_token_count;`
- **用户消息压缩预算绝对值写死**：`const COMPACT_USER_MESSAGE_MAX_TOKENS: usize = 20_000;`
- **超窗处置=先裁最旧再重试**：`matches!(e.details(), CodexErrorDetails::ContextWindowExceeded) => { ... history.remove_first_item(); retries = 0; continue; }`（装不下则 `set_total_tokens_full` 后 `return Err(e)`）。
- 另有 **token-budget 压缩"装新窗口"**：`codex-rs/core/src/compact_token_budget.rs` —— doc："Token-budget compaction skips model/server summarization and installs a fresh context window instead."，`run_manual_compact_task` / `run_inline_auto_compact_task → run_compact_task_inner → sess.start_new_context_window(...)`。
- （单次输出上限本轮未取到写死常量——由 Rust core 请求层/模型默认决定，**未核实**。）

### 1.4 aaif-goose/goose @main（原 block/goose 已改名，Rust）

- **窗口不写死，运行期解析**：`crates/goose/src/context_limit.rs`
  - `pub async fn get_context_limit(provider, model) { let override_limit = Config::global().get_goose_context_limit()?; Ok(provider.get_context_limit(model, override_limit).await) }`
  - 声明式 provider 的 `model.context_limit` 经 `ContextLimitResolver::new(...).with_configured_limits(...).resolve_local(model, override_limit)` 解析。
- **压缩阈值=窗口百分比 + 绝对上限取小**：
  - `pub const DEFAULT_COMPACTION_THRESHOLD: f64 = 0.8;`（`crates/goose-context-management/src/lib.rs`）
  - `pub const DEFAULT_AUTO_COMPACT_TOKEN_LIMIT: usize = 225_000;`（`crates/goose/src/context_mgmt/mod.rs:25`）
    - 注释原文（`mod.rs:23-24`）："Starts compaction with headroom below OpenAI's long-context price tier (above 272k input tokens). **This is not a maximum request size.**"
  - 合流：`threshold.min(token_limit as f64 / context_limit as f64)`（`mod.rs:41`，函数 `auto_compact_threshold`，`mod.rs:30-42`）
  - 判定：`usage_ratio = current_tokens as f64 / context_limit as f64; needs_compaction = ... usage_ratio > threshold`（`mod.rs:283-289`）——**只用输入侧 current_tokens，未减输出额度**。
  - token 来源：有 `session.usage.total_tokens` 用真实用量，否则 `create_token_counter()` 估算（`mod.rs:266-281`）——真分词器。
  - 工具批裁剪：`compute_tool_call_cutoff = (3 * effective_limit / 20_000).clamp(10, 500)`（`mod.rs:383-391`，`effective_limit = context_limit * threshold`）。
  - 超窗处置：provider 报 `ProviderError::ContextLengthExceeded` 时**渐进移除最旧消息再重试**（`mod.rs` 测试 `test_progressive_removal_on_context_exceeded`）。
- （单次输出上限由 `ModelConfig.max_tokens` 承载、可按 provider/模型配置，未见全局写死默认值——**未核实**。）

### 1.5 continuedev/continue @main（TS）

- **`core/llm/constants.ts`**：
  - `const DEFAULT_MAX_TOKENS = 4096;`（:1，单次输出上限写死默认，可 per-model 覆盖）
  - `const DEFAULT_CONTEXT_LENGTH = 32_768;`（:2，模型自带 contextLength 缺失时的**兜底**窗口）
  - `const DEFAULT_PRUNING_LENGTH = 128000;`（:4）
  - `const DEFAULT_REASONING_TOKENS = 2048;`（:5，**单独列出的推理预算**）
  - `const DEFAULT_MAX_CHUNK_SIZE = 500; // 512 - buffer for safety (in case of differing tokenizers)`（:14）
- **`core/llm/getAdjustedTokenCount.ts` 安全系数**（近似分词器 + 模型专属乘子，非精确 tiktoken）：
  - `const ANTHROPIC_TOKEN_MULTIPLIER = 1.23;` / `GEMINI_TOKEN_MULTIPLIER = 1.18;` / `MISTRAL_TOKEN_MULTIPLIER = 1.26;`
  - `getAdjustedTokenCountFromModel`：默认 multiplier=1，claude→1.23，gemini→1.18，mistral 家族→1.26；`return Math.ceil(baseTokens * multiplier)`。

### 1.6 明确搜不到 / 已变 / 无源码（不替换成记忆答案）

- **anthropics/claude-code**：仓库根目录本次仅 `CHANGELOG.md / README / CLAUDE.md / examples / plugins / scripts / feed.xml`；Agent 以 minified npm bundle 分发、**无源码**，无法核实任何 token 常量 → **未核实**。
- **RooCodeInc/Roo-Code**：仓库元数据返回 **`archived: true`**（最后 push 约 2026-05-15）→ 已归档。
- **All-Hands-AI/OpenHands**：302 重定向到 `OpenHands/OpenHands`，`main` 根目录已是 TS 前端，`contents/openhands/memory` 返回 404，condenser 已拆分 → **未取到 condenser 源码，未核实**。
- **Cline（cline/cline）**：已 monorepo 化（`apps/sdk/docs/evals`），`src/core/context-management` 等路径 404 → **未定位到常量文件，未核实**。

---

## 2. 共性 vs 分歧

**共识（多家一致）：**
1. 输出上限是"模型属性"，优先查表/按模型配（SWE-agent `models.py:604-607`、aider `models.py:162-165`），continue 虽有默认 4096 仍可 per-model 覆盖（`constants.ts:1`）。
2. 窗口优先用"模型/服务商元数据"，客户端常量只兜底（goose `context_limit.rs`、continue `constants.ts:2`）——**区分"模型真实窗口"与"客户端自设阈值"**。
3. 压缩触发普遍用窗口百分比，且再叠一个绝对上限取更小（goose `DEFAULT_COMPACTION_THRESHOLD=0.8` + `min(比率, 225000/窗口)`，`mod.rs:41`）。
4. 估算要么真分词器（SWE-agent `models.py:690-694`、goose `mod.rs:266-281`），要么近似但乘安全系数（codex `approx_token_count`、continue 1.18~1.26）——**宁可高估占用**。
5. 撞墙偏向先自救再失败（codex `remove_first_item` 重试、goose 渐进移除重试）。

**分歧（别当统一标准）：**
1. 超窗重不重试两派：codex/goose 重试 vs **SWE-agent 明确不重试**（`models.py:809-831`）。
2. **压缩预算里扣不扣本次输出额度：本次核到没有一家显式减** `max_tokens`（SWE-agent `models.py:701-703` 只比输入、goose `mod.rs:283-289` 只用 current_tokens）；continue 把推理预算单列（`constants.ts:5`）算"拆出来单独管"，但也没并进压缩判定。**这是全行业共同留的坑，CoreCoder 应主动补。**
3. 裁剪粒度：aider 绝对值夹窗口 1/16（`models.py:355-358`）、codex 压到绝对 20000、SWE-agent/goose 按条/按批。

---

## 3. CoreCoder 差距分析（对照现状，均为只读引用）

| 维度 | CoreCoder 现状（文件:行） | 业界参照 | 差距 |
|---|---|---|---|
| 输出上限是否按模型 | `config.py:31 max_tokens=4096`、`:51 CORECODER_MAX_TOKENS 默认 "4096"` —— 全局写死、与 model 无关 | SWE-agent/aider 查 litellm 表；continue 默认+per-model 覆盖 | 换模型不自动适配，需手动改 env（属"手动查表"，需自觉） |
| 窗口区分真值/阈值 | `config.py:33/:53 max_context_tokens=128_000` —— 单一常量既当窗口又当阈值 | goose/continue 分离真窗口 vs 客户端阈值 | 二者混成一个 128000，易误设 |
| 压缩触发 | `context.py:54-56` snip 0.50 / summarize 0.70 / collapse 0.90（已是窗口百分比） | goose 比率之上再叠绝对上限取小 | 只有百分比，**无绝对头寸上限兜底** |
| 预算是否扣输出额度 | `context.py:66 current = estimate_tokens(messages) + fixed_tokens`，`fixed_tokens` 来自 `agent.py:254 _fixed_prompt_tokens`（system+schemas），**未减 max_tokens** | 业界普遍也没减（见 2.分歧2） | **共同坑**：0.90 触发时若 输入+fixed+max_tokens > 真窗口，服务端仍 400 |
| 估算精度 | `context.py:30-37 _approx_tokens` 字符近似（CJK/1.5、符号密集/2.8、其余/3.4），**无对外安全系数** | SWE-agent/goose 真分词；continue ×1.18~1.26 | 更易低估占用 |
| 截断（finish_reason=length） | `agent.py:313-343` M2 guard；`llm.py:139-141 max_tokens→max_completion_tokens` 方言适配 | 与 codex/goose"撞墙自救"同方向 | CoreCoder 选择明确报错并提示调 `CORECODER_MAX_TOKENS`，保持即可 |
| 输入超窗 | `agent.py:370-372, 390-408 _recover_overflow`：报超窗 → `force_collapse` 折一次再重试一次 | 与 codex `remove_first_item` 重试同族 | 对，但只重试一次 |

---

## 4. 配置公式与建议取值

### 4.1 红线：三个本地模型的"真实窗口/最大输出"本文不凭记忆给数

`mimo-v2.6-flash` / `deepseek-v4.1-flash` / `kimi-k3.1-por` 的 `W_in`（输入窗口）、`C_out`（最大完成 tokens）**本次均未检索到**。SWE-agent/aider 恰恰是**运行时查 litellm 表**得到这些数（`SWE-agent models.py:599-607`、`aider models.py:162-165`），而非写死。请按 **litellm `model_prices_and_context_window.json` 或服务商控制台/模型卡**取数，记作 `W_in`、`C_out`，再套公式。

> `.env:13` 注记 `# provider ceiling is 131072 completion tokens` 是用户自己的注释、**非本次检索到的事实**，下文仅作占位演示，最终用服务商实际数字替换。

### 4.2 公式

- **输出侧 `CORECODER_MAX_TOKENS`**：取 `≈ C_out 的 1/8 ~ 1/4`，别顶满。
  - 以占位 `C_out=131072` 为例：`.env` 现值 `16384 = C_out/8` 落在保守工作点（与 continue 默认 4096、SWE-agent Claude 特例 64000 同量级）；若工具调用常被判截断，可上调到 `32768`（仍 `≤ C_out/4`），**不要设成 131072**。
- **输入侧 `CORECODER_MAX_CONTEXT`（核心公式）**：
  \[ \text{CORECODER\_MAX\_CONTEXT} \approx W_{in} - \text{CORECODER\_MAX\_TOKENS} - \text{安全余量} \]
  - 这一步补上 `context.py:66` 没减 `max_tokens` 的缺口。
  - 占位 `W_in=131072`、`MAX_TOKENS=16384` → `131072 − 16384 = 114688`，再向下取整留余量到 **约 `100000`**；此时 `context.py:56` 的 0.90 collapse 点 ≈ `90000`，加输出 16384 仍在 131072 内。
  - **反例警告**：沿用默认 128000（`.env` 未设 `CORECODER_MAX_CONTEXT` → `config.py:53` 回落 128000），当 `W_in=131072` 时 `0.90×128000=115200` 输入 + `16384` 输出 = `131584 > 131072`，**恰好越界**，`agent.py:370 _recover_overflow` 白折一次。

### 4.3 余量安全边界的检索事实来源（非拍脑袋）

- goose 停在 272k 天花板之下留头寸、并明确"**This is not a maximum request size**"（`mod.rs:23-25`）→ 别把标称最大值当工作点。
- continue 用近似分词器 + `×1.18~1.26` 安全系数（`getAdjustedTokenCount.ts`）、chunk 尺寸 `512 - buffer for safety`（`constants.ts:14`）→ 估算偏保守；CoreCoder `_approx_tokens`（`context.py:30-37`）纯字符近似无系数，更该留余量。
- 压缩阈值不减输出额度是全行业共同坑（见 2.分歧2）→ 主动在 `MAX_CONTEXT` 里减掉 `MAX_TOKENS` 是优于现状的做法。

### 4.4 换 deepseek / kimi 的调法

- 同公式，仅替换 `W_in/C_out`。二者若为**推理模型**，参考 continue 单列推理预算（`constants.ts:5 DEFAULT_REASONING_TOKENS=2048`）：`CORECODER_MAX_TOKENS` 需**同时覆盖思考链+最终回答**，应比非推理模型更宽；且思考 tokens 也吃窗口（CoreCoder `llm.py:188-191` reasoning 不入历史但仍计入服务端输出），压缩阈值更保守。
- 若某模型 `W_in` 明显更大（如 200k 档），**不要把 `MAX_CONTEXT` 线性顶到 0.9×大窗**——沿 goose"再叠绝对上限取小"（`mod.rs:41`）给成本/延迟留头寸。

---

## 5. 可复算验证方法（每个数字落到脚本/测试输出）

**A. 输出截断被防护（M2 guard）：**
```
.venv\Scripts\python.exe -m pytest tests/test_agent_loop_hardening.py -k "truncated or finish_reason or consecutive" -q
```
- 用例钉：`test_agent_loop_hardening.py:129`（`llm.py:282-283` finish_reason 采集）、`:154`（`agent.py:319-321` 截断不执行）、`:184`（`agent.py:438-443` 连两次抛 `TruncatedOutput`）。
- 实跑：`CORECODER_MAX_TOKENS=64 .venv\Scripts\python.exe examples/m2_truncated_tool_call.py` → 审计出现 `output_truncated`（`agent.py:435`），tool 未执行，末尾文案 "raise max_tokens (CORECODER_MAX_TOKENS)"（`agent.py:341`/`441`）。

**B. 输入超窗被压缩 / 超窗折一次重试：**
```
.venv\Scripts\python.exe -m pytest tests/test_agent_loop_hardening.py -k "overflow or fixed_overhead or compression" -q
.venv\Scripts\python.exe -m pytest tests/test_core.py -k "context_snip or context_compress or orphan_tool" -q
```
- 钉 `context.py:66`（阈值含 fixed）：`:663 test_compression_counts_fixed_overhead`、`:677 test_agent_counts_fixed_overhead_in_compression`。
- 钉 `agent.py:370-408 _recover_overflow`：`:605`（触发 force_collapse 并重试）、`:621`（只重试一次）、`:650`（短历史不折）。
- 分层压缩/孤儿 tool：`test_core.py:82/:93/:120`。
- 端到端：`examples/m3_context_overflow.py:194 max_context_tokens=20_000_000` 是**故意调大到不压缩、专演撞服务商墙后 recover**；要演"输入侧提前压缩"用 `CORECODER_MAX_CONTEXT=2000 CORECODER_MAX_TOKENS=512 .venv\Scripts\python.exe examples/m3_context_overflow.py` → 审计应出现 `context_compressed`（`context.py:73/81/89` layer=snip/summarize/collapse）先于任何 400。

**C. 回归全量（改动前后确认防护不破坏）：**
```
.venv\Scripts\python.exe -m pytest tests/test_agent_loop_hardening.py tests/test_core.py tests/test_audit.py -q
```
- `test_audit.py:406 / :427 / :827` 保证压缩 / 超窗恢复 / 截断三类事件在审计里可核对。

---

## 6. 后续可执行清单（本文只记录，不实施）

1. 用 litellm 表 / 服务商模型卡查得当前 `CORECODER_MODEL` 的 `W_in`、`C_out`（**不要凭记忆**）。
2. 依 §4.2 公式推导并显式设置 `CORECODER_MAX_CONTEXT`（当前 `.env` 未设，回落 `config.py:53` 的 128000）。
3. 若要根治"压缩预算不含输出"：让 `context.py:66` 的判定加上 `max_tokens`（当前 `agent.py:310-311/333-334` 只传 `fixed_tokens`）——需配套 §5-B 的 `fixed_overhead` 用例扩展。
4. 若要贴近 continue 的估算可靠性：给 `_approx_tokens`（`context.py:30-37`）加对外安全系数，或改用真实 tokenizer。

> 以上均为只读记录；任何改动需另行确认后再动 `corecoder/` 源码或 `.env`。
