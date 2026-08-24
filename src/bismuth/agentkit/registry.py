"""A name-keyed set of tools the agent can reach."""

from __future__ import annotations

from collections.abc import Iterable

from bismuth.agentkit.messages import ToolSpec
from bismuth.agentkit.tool import Tool, tool_spec


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

    def all(self) -> list[Tool]:
        """Every tool, in the order they were added."""
        return list(self._by_name.values())

    def specs(self) -> list[ToolSpec]:
        return [tool_spec(t) for t in self._by_name.values()]

    def __len__(self) -> int:
        return len(self._by_name)
