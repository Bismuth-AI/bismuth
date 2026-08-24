"""Token estimation and transcript compaction."""

from __future__ import annotations

from collections.abc import Sequence

from bismuth.agentkit.messages import Message, ToolSpec

CLEARED = "[Old tool result cleared to save context. Call the tool again if you need it.]"

MESSAGE_OVERHEAD_TOKENS = 4
"""Per-message wire framing (role, delimiters) that a count of the text cannot see."""


BYTES_PER_TOKEN = 2.7
"""Conservative UTF-8 byte-to-token estimate."""


def estimate(text: str) -> int:
    """How many tokens a string costs, erring high. See ``BYTES_PER_TOKEN``."""
    return int(len(text.encode("utf-8")) / BYTES_PER_TOKEN) + 1


def message_tokens(message: Message) -> int:
    total = estimate(message.content) + MESSAGE_OVERHEAD_TOKENS
    for call in message.tool_calls:
        total += estimate(call.name) + estimate(str(call.arguments))
    return total


def transcript_tokens(
    system: str, messages: Sequence[Message], tools: Sequence[ToolSpec] = ()
) -> int:
    """What one request would cost: the prompt, the tool schemas, and the transcript."""
    total = estimate(system)
    for spec in tools:
        total += estimate(spec.name) + estimate(spec.description) + estimate(str(spec.parameters))
    return total + sum(message_tokens(message) for message in messages)


def since(anchor_tokens: int, messages: Sequence[Message], at: int) -> int:
    """Add estimated new messages to a provider-counted prefix."""
    return anchor_tokens + sum(message_tokens(m) for m in messages[at:])


def fit_batch(results: list[str], *, limit_chars: int) -> list[str]:
    """Clip the largest results until the batch fits within ``limit_chars``."""
    total = sum(len(r) for r in results)
    if total <= limit_chars or not results:
        return results
    out = list(results)
    share = max(500, limit_chars // len(results))
    for index in sorted(range(len(out)), key=lambda i: len(out[i]), reverse=True):
        if total <= limit_chars:
            break
        smaller = clip(out[index], limit_chars=share)
        total -= len(out[index]) - len(smaller)
        out[index] = smaller
    return out


def clip(text: str, *, limit_chars: int, head: int = 60, tail: int = 20) -> str:
    """Head and tail of an over-long tool result, with the middle marked as dropped."""
    if len(text) <= limit_chars:
        return text
    lines = text.splitlines()
    if len(lines) <= head + tail:
        # Fall back to character clipping when line clipping cannot help.
        return (
            text[:limit_chars]
            + f"\n… [cut here; {len(text)} characters in total. Read a narrower range for the rest.]"
        )
    dropped = len(lines) - head - tail
    middle = f"… [{dropped} lines omitted. Read them by asking for a narrower range.] …"
    return "\n".join([*lines[:head], middle, *lines[-tail:]])


def microcompact(messages: list[Message], *, keep_recent: int, need: int | None = None) -> int:
    """Clear old tool results until ``need`` tokens are freed."""
    clearable = [
        index
        for index, message in enumerate(messages)
        if message.role == "tool" and message.content != CLEARED
    ]
    spare = len(clearable) - max(1, keep_recent)
    freed = 0
    for index in clearable[: max(0, spare)]:
        if need is not None and freed >= need:
            break
        old = messages[index]
        freed += estimate(old.content) - estimate(CLEARED)
        messages[index] = Message("tool", CLEARED, tool_call_id=old.tool_call_id)
    return freed


def shrink(messages: list[Message], *, limit_chars: int) -> int:
    """Clip every tool result to ``limit_chars`` and return freed tokens."""
    freed = 0
    for index, message in enumerate(messages):
        if message.role != "tool" or len(message.content) <= limit_chars:
            continue
        smaller = clip(message.content, limit_chars=limit_chars)
        freed += estimate(message.content) - estimate(smaller)
        messages[index] = Message("tool", smaller, tool_call_id=message.tool_call_id)
    return freed


def evict(messages: list[Message], *, need: int, keep_last: int = 1) -> int:
    """Evict a valid transcript prefix until ``need`` tokens are freed."""
    cut, freed = 0, 0
    limit = len(messages) - keep_last
    while cut < limit and freed < need:
        freed += message_tokens(messages[cut])
        cut += 1
        # Do not stop between an assistant's tool calls and their results.
        while cut < limit and messages[cut].role == "tool":
            freed += message_tokens(messages[cut])
            cut += 1
    # A result left at the front would answer a call that is no longer in the transcript.
    while cut and cut < len(messages) - 1 and messages[cut].role == "tool":
        freed += message_tokens(messages[cut])
        cut += 1
    del messages[:cut]
    return freed
