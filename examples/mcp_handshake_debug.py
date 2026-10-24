"""examples/mcp_handshake_debug.py
只真实测试 MCP 阶段 1-3：initialize / notifications/initialized / tools/list。
不调 LLM、不执行任何工具。读的是 ~/.corecoder/mcp.json 里你配的那 3 个 server。"""

import logging

from corecoder.mcp import _live_clients, load_mcp_tools


def main() -> int:
    # 让起不来的 server 把原因打出来（load_mcp_tools 内部只 log warning，不抛）
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    tools = load_mcp_tools()  # ← 这一行内部就完成阶段 1-3（对每个 server 各一遍）

    print(f"loaded {len(tools)} MCP tools from {len(_live_clients)} servers")
    for c in _live_clients:
        state = "dead" if c.error else "alive"
        print(f"  server {c.name!r}: {state}  tools={len(c.tools)}  error={c.error}")
    for t in tools:
        # 阶段3 的产物：每个远程工具被包成 mcp__<server>__<tool>，带 JSON Schema
        print("   -", t.name, "|", t.description[:60])
        print("     schema:", t.schema()["function"]["parameters"])

    for c in _live_clients:
        c.close()  # 顺手验证优雅关停
    return 0


if __name__ == "__main__":
    raise SystemExit(main())