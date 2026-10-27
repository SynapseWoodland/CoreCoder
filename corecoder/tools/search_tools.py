"""On-demand tool loading (the industry's "tool search" / deferred loading).

When many MCP tools are attached, their full JSON schemas ride along in every
request's ``tools=`` even though a task usually needs one or two of them.
Rather than ship the whole catalog every round, the agent exposes only a
compact index plus this one meta-tool: the model calls ``search_tools`` with a
keyword and the matching tools' full schemas join the request from the next
round on.

This is the opposite of trimming a description -- nothing is cut, so tool
selection gets easier (fewer irrelevant candidates in the structured array),
not harder. It mirrors what Claude Code ships ("MCP tools ... deferred and
discovered via the MCPSearch tool instead of being loaded upfront").
"""

from typing import ClassVar

from .base import Tool


class SearchToolsTool(Tool):
    name = "search_tools"
    description = (
        "Load the full definition of external (MCP) tools listed under "
        "'Deferred tools' that are not callable yet. Pass a keyword (a tool "
        "name or a capability); matching tools become callable on the next "
        "step. Search again if you need a different set."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keyword matched against deferred tool names and descriptions",
            },
        },
        "required": ["query"],
    }

    # wired by Agent.__init__ only while deferral is active
    _agent = None

    def __init__(self, agent=None):
        self._agent = agent

    def execute(self, query: str = "") -> str:
        if self._agent is None:
            return "Error: search_tools not initialized (no agent)"
        loaded = self._agent._activate_matching(query)
        if not loaded:
            return (
                "No matching deferred tools; nothing new was loaded. The "
                "'Deferred tools' index in the system prompt lists what is "
                "available - try a different keyword."
            )
        return (
            f"Loaded {len(loaded)} tool(s), now callable: "
            + ", ".join(loaded)
        )
