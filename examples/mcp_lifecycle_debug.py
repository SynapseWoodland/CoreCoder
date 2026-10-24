"""examples/mcp_lifecycle_debug.py
离线单步走完 MCP 全生命周期：握手 -> tools/list -> tool_call -> execute -> 结果回灌 -> 循环收尾。
不需要 API key，不花 token。"""

import json
import sys
import tempfile
from pathlib import Path

from corecoder.agent import Agent
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.mcp import MCPClient

# 一个只用标准库的假 MCP server（Windows 可直接跑，抄自 tests/test_mcp.py）
FAKE_SERVER = """
import json, sys
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8", newline="\\n")
TOOLS = [{"name": "echo", "description": "Echo text back",
          "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                          "required": ["text"]}}]
for line in sys.stdin:
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue
    if "id" not in req:
        continue  # notification: nothing to answer (阶段2)
    method, params = req.get("method"), req.get("params") or {}
    if method == "initialize":            # 阶段1
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake", "version": "0.1"}}
    elif method == "tools/list":          # 阶段3
        result = {"tools": TOOLS}
    elif method == "tools/call":          # 阶段4/5
        args = params.get("arguments") or {}
        result = {"content": [{"type": "text", "text": "echo: " + args.get("text", "")}],
                  "isError": False}
    else:
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}) + "\\n")
    sys.stdout.flush()
"""


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mcp-debug-"))
    script = workdir / "fake_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")

    # ---- 阶段 1+2+3：构造 client 即完成 initialize / initialized / tools/list ----
    client = MCPClient("fake", sys.executable, [str(script)])
    print("[1-3] registered tools:", [t.name for t in client.tools])
    echo = next(t for t in client.tools if t.name == "mcp__fake__echo")
    print("[3] schema sent to model:\n", json.dumps(echo.schema(), indent=2))

    # ---- 阶段 4-6：喂一个 tool_call 顶替"推理"，看它执行并回灌、收尾 ----
    agent = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(id="c1", name="mcp__fake__echo",
                                             arguments={"text": "hi"})]),
            LLMResponse(content="done"),
        ]),
        tools=[echo],  # permission=None -> agent.py 的 _permit 直接放行
    )
    result = agent.chat("please echo hi")
    print("[6] final reply:", result)
    print("--- conversation context ---")
    for i, m in enumerate(agent.messages):
        print(f"[{i}]", json.dumps(m, ensure_ascii=False))

    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())