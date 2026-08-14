"""A name-keyed set of tools the agent can reach."""

from __future__ import annotations

from collections.abc import Iterable

from agentkit.messages import ToolSpec
from agentkit.tool import Tool, tool_spec


class ToolRegistry:
    """Tools by name, plus the specs the model is shown."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._by_name: dict[str, Tool] = {}
        for t in tools:
            self.add(t)

    def add(self, tool: Tool) -> None:
        if tool.name in self._by_name:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._by_name[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._by_name.get(name)

    def specs(self, names: set[str] | None = None) -> list[ToolSpec]:
        """Model-facing specs, optionally restricted during a conclusion phase."""
        return [
            tool_spec(tool)
            for name, tool in self._by_name.items()
            if names is None or name in names
        ]

    def __len__(self) -> int:
        return len(self._by_name)
