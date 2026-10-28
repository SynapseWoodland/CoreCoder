"""Base class for all tools."""

import threading
from abc import ABC, abstractmethod

# File-mutating tools (write_file/edit_file) serialize on this, so a parallel
# batch can't interleave read-modify-write on the same file and lose an edit.
FILE_MUTATION_LOCK = threading.Lock()


class ToolRefused(str):
    """A tool result that is a refusal rather than an execution outcome.

    It stays a ``str`` so every existing consumer (history, hooks, tests) sees
    plain text, while the audit layer can tell "the tool said no" apart from
    "the tool ran and returned a warning"."""


class SubagentTruncated(str):
    """A sub-agent result that died on repeated output truncation (G5).

    Same trick as :class:`ToolRefused`: plain text for the model and the
    hooks, while the audit layer marks the parent's ``tool_result`` line with
    ``error_type=TruncatedOutput`` instead of letting the child's failure
    read like any other answer."""


class Tool(ABC):
    """Minimal tool interface. Subclass this to add new capabilities."""

    name: str
    description: str
    parameters: dict  # JSON Schema for the function args

    @abstractmethod
    def execute(self, **kwargs) -> str:
        """Run the tool and return a text result."""
        ...

    def schema(self) -> dict:
        """OpenAI function-calling schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
