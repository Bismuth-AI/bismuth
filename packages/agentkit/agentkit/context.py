"""Active-context management for long-running tool agents.

The raw transcript remains in ``RunResult``. Only the projection sent back to the
model is compacted: large and old tool observations move into an addressable archive
and can be paged back with ``recall_tool_result``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from agentkit.messages import Message, ToolCall, ToolSpec
from agentkit.tool import FunctionTool, Tool


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    """Limits for active model context, never for the preserved raw trace."""

    max_active_tokens: int = 24_000
    max_inline_tool_tokens: int = 8_000
    keep_recent_tool_results: int = 8
    recall_page_chars: int = 12_000
    repeated_call_limit: int = 2

    def __post_init__(self) -> None:
        for name in (
            "max_active_tokens",
            "max_inline_tool_tokens",
            "keep_recent_tool_results",
            "recall_page_chars",
            "repeated_call_limit",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class ContextCompaction:
    archived_results: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    still_over_limit: bool = False


@dataclass(frozen=True, slots=True)
class _ArchivedResult:
    tool: str
    content: str


class _RecallArgs(BaseModel):
    result_id: str = Field(description="R-prefixed archived tool-result ID.")
    offset: int = Field(default=0, ge=0, description="First character to return.")
    limit: int = Field(default=12_000, ge=1, le=20_000, description="Characters to return.")


def estimate_tokens(value: str) -> int:
    """Conservative tokenizer-free estimate, including CJK-heavy observations."""

    ascii_chars = sum(1 for char in value if ord(char) < 128)
    return (ascii_chars + 3) // 4 + (len(value) - ascii_chars)


def tool_call_key(call: ToolCall) -> str:
    """Stable signature for detecting an identical non-progressing call."""

    return f"{call.name}:{json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)}"


class ActiveContext:
    """Per-run observation archive and compact model-facing projection."""

    def __init__(self, policy: ContextPolicy) -> None:
        self.policy = policy
        self._items: dict[str, _ArchivedResult] = {}
        self._content_ids: dict[tuple[str, str], str] = {}
        self._next_id = 1

    def reset(self) -> None:
        """Start a fresh run while keeping the recall tool wired to this archive."""

        self._items.clear()
        self._content_ids.clear()
        self._next_id = 1

    def recall_tool(self) -> Tool:
        async def _recall(args: _RecallArgs) -> str:
            result_id = args.result_id.upper()
            item = self._items.get(result_id)
            if item is None:
                return f"No archived tool result: {args.result_id}"
            limit = min(args.limit, self.policy.recall_page_chars)
            window = item.content[args.offset : args.offset + limit]
            end = args.offset + len(window)
            trailer = (
                f"\n… result {result_id} has {len(item.content)} characters; "
                f"call again with offset={end}"
                if end < len(item.content)
                else f"\n(end of {result_id})"
            )
            return window + trailer

        return FunctionTool(
            name="recall_tool_result",
            description=(
                "Page an earlier tool observation moved out of active context. Use "
                "the short R-prefixed ID shown in its placeholder."
            ),
            params=_RecallArgs,
            handler=_recall,
            read_only=True,
            concurrency_safe=True,
        )

    def project_result(self, tool: str, content: str) -> str:
        """Archive and preview an unusually large observation."""

        if estimate_tokens(content) <= self.policy.max_inline_tool_tokens:
            return content
        result_id = self._store(tool, content)
        keep = max(self.policy.recall_page_chars // 4, 400)
        head = content[:keep]
        tail = content[-keep:] if len(content) > keep else ""
        middle = "\n… [middle archived] …\n" if tail else ""
        return (
            f"[Archived tool result {result_id} from {tool}; {len(content)} characters. "
            "Use recall_tool_result for exact paginated content.]\n"
            f"{head}{middle}{tail}"
        )

    def compact(
        self,
        messages: list[Message],
        *,
        system: str,
        tools: list[ToolSpec],
    ) -> ContextCompaction:
        """Offload oldest observations until the active projection fits."""

        before = self._active_tokens(messages, system=system, tools=tools)
        if before <= self.policy.max_active_tokens:
            return ContextCompaction(tokens_before=before, tokens_after=before)

        tool_indexes = [index for index, message in enumerate(messages) if message.role == "tool"]
        candidates = tool_indexes[: -self.policy.keep_recent_tool_results]
        archived = self._compact_indexes(messages, candidates)
        after = self._active_tokens(messages, system=system, tools=tools)
        if after > self.policy.max_active_tokens:
            remaining = tool_indexes[:-2]
            archived += self._compact_indexes(messages, remaining)
            after = self._active_tokens(messages, system=system, tools=tools)
        if after > self.policy.max_active_tokens:
            archived += self._compact_indexes(messages, tool_indexes)
            after = self._active_tokens(messages, system=system, tools=tools)

        return ContextCompaction(
            archived_results=archived,
            tokens_before=before,
            tokens_after=after,
            still_over_limit=after > self.policy.max_active_tokens,
        )

    def _compact_indexes(self, messages: list[Message], indexes: list[int]) -> int:
        archived = 0
        for index in indexes:
            message = messages[index]
            if message.content.startswith("[Archived tool result "):
                match = re.match(r"\[Archived tool result (R\d+) from ([^;]+);", message.content)
                if match is not None and len(message.content) > 500:
                    result_id, tool = match.groups()
                    item = self._items.get(result_id)
                    exact_chars = len(item.content) if item is not None else len(message.content)
                    messages[index] = Message(
                        "tool",
                        (
                            f"[Archived tool result {result_id} from {tool}; {exact_chars} "
                            "characters. Use recall_tool_result for exact content.]"
                        ),
                        tool_call_id=message.tool_call_id,
                    )
                    self._compact_call_arguments(messages, message.tool_call_id, result_id)
                    archived += 1
                continue
            tool = self._tool_name(messages, message.tool_call_id) or "unknown"
            result_id = self._store(tool, message.content)
            preview = " ".join(message.content.split())[:240]
            messages[index] = Message(
                "tool",
                (
                    f"[Archived tool result {result_id} from {tool}; "
                    f"{len(message.content)} characters. Use recall_tool_result for exact "
                    f"content. Preview: {preview}]"
                ),
                tool_call_id=message.tool_call_id,
            )
            self._compact_call_arguments(messages, message.tool_call_id, result_id)
            archived += 1
        return archived

    @staticmethod
    def _compact_call_arguments(
        messages: list[Message], call_id: str | None, result_id: str
    ) -> None:
        """Drop bulky historical arguments after their observation is addressable."""

        if call_id is None:
            return
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if not message.tool_calls:
                continue
            changed = False
            calls: list[ToolCall] = []
            for call in message.tool_calls:
                if call.id == call_id:
                    calls.append(
                        ToolCall(
                            id=call.id,
                            name=call.name,
                            arguments={"archived_result": result_id},
                        )
                    )
                    changed = True
                else:
                    calls.append(call)
            if changed:
                messages[index] = Message(message.role, "", tuple(calls))
                return

    def _store(self, tool: str, content: str) -> str:
        key = (tool, content)
        if existing := self._content_ids.get(key):
            return existing
        result_id = f"R{self._next_id:06d}"
        self._next_id += 1
        self._items[result_id] = _ArchivedResult(tool=tool, content=content)
        self._content_ids[key] = result_id
        return result_id

    @staticmethod
    def _tool_name(messages: list[Message], call_id: str | None) -> str | None:
        if call_id is None:
            return None
        for message in reversed(messages):
            for call in message.tool_calls:
                if call.id == call_id:
                    return call.name
        return None

    @staticmethod
    def _active_tokens(
        messages: list[Message], *, system: str, tools: list[ToolSpec]
    ) -> int:
        total = estimate_tokens(system)
        for spec in tools:
            total += estimate_tokens(spec.name + spec.description)
            total += estimate_tokens(json.dumps(spec.parameters, ensure_ascii=False))
        for message in messages:
            total += estimate_tokens(message.content)
            for call in message.tool_calls:
                total += estimate_tokens(call.name)
                total += estimate_tokens(json.dumps(call.arguments, ensure_ascii=False))
        return total
