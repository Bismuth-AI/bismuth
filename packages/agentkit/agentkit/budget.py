"""Keeping a run inside the model's context window.

A turn cap is the wrong instrument. It is a guess about cost made before anything is
known about the question: it cuts off a cheap question that needed one more look, and
lets an expensive one run until the provider refuses the request. What actually runs
out is the context window, so that is what this measures.

Three moves, in the order they cost the run something:

1. **Clip** an over-long tool result as it arrives. A sidecar can be a hundred thousand
   characters; once one lands whole in the transcript it is paid for on every turn
   afterwards. The head and tail go in, the middle is marked as dropped, and the model
   is told how to fetch the rest.
2. **Microcompact** when the transcript approaches the ceiling: blank the *content* of
   the oldest tool results, keeping the most recent ones. Tool results are the only
   thing cleared, because they are the only thing the model can get back by asking
   again -- its own reasoning and the user's words are not re-obtainable. Keeping the
   recent ones matters: an agent whose every result has been wiped has no working
   context and starts its search from the beginning.
3. **Evict** from the front, as a last resort, when clearing results was not enough.
   Whole messages go, oldest first, and always in one prefix so that no tool result is
   left answering a call that is no longer there.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentkit.messages import Message, ToolSpec

CLEARED = "[Old tool result cleared to save context. Call the tool again if you need it.]"

MESSAGE_OVERHEAD_TOKENS = 4
"""Per-message wire framing (role, delimiters) that a count of the text cannot see."""


BYTES_PER_TOKEN = 2.7
"""Divisor for the byte-based estimate.

Counted in UTF-8 bytes rather than characters because the documents are Korean: a
Hangul syllable is three bytes and about one token, while ASCII is one byte and about
a quarter. Three looked right on that reasoning and was wrong: measured against the
provider's own count over fifteen real requests it ran 0.90-1.05 of the truth, and the
error grew with size -- 0.90 at 8.8k tokens. Under-counting is the dangerous direction,
so the divisor is set where the worst observed case lands at 1.0 rather than where the
reasoning said it should be."""


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
    """A measured count, plus an estimate of only what has happened since.

    The transcript's true size is known exactly for the prefix the provider last
    counted; guessing at the whole thing throws that away and compounds the estimate's
    error over every turn. ``at`` is how many messages were in the request that
    returned ``anchor_tokens``.
    """
    return anchor_tokens + sum(message_tokens(m) for m in messages[at:])


def fit_batch(results: list[str], *, limit_chars: int) -> list[str]:
    """Clip the largest of one turn's results until they fit together.

    A per-result cap does not bound a turn: several tools running in parallel, each
    just under the cap, still arrive as one message. The largest give way first, so
    a turn that read one big thing and three small ones keeps the small ones whole.
    """
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
        # Few, enormous lines: there is nothing to cut between, so cut the text itself.
        return (
            text[:limit_chars]
            + f"\n… [cut here; {len(text)} characters in total. Read a narrower range for the rest.]"
        )
    dropped = len(lines) - head - tail
    middle = f"… [{dropped} lines omitted. Read them by asking for a narrower range.] …"
    return "\n".join([*lines[:head], middle, *lines[-tail:]])


def microcompact(messages: list[Message], *, keep_recent: int, need: int | None = None) -> int:
    """Blank the oldest tool results in place. Returns the tokens this freed.

    Stops as soon as ``need`` tokens have been freed, so a run that is barely over the
    ceiling loses only its oldest look rather than all of them. ``need=None`` clears
    everything eligible.
    """
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
    """Clip every tool result down to ``limit_chars``. Returns the tokens this freed.

    The reactive fallback, for when the provider refuses a request that the estimate
    said would fit. Unlike ``microcompact`` this touches the most recent result too --
    a run whose single look is itself too big has nothing else to give up -- but it
    clips rather than clears, so the model keeps both ends of what it read.
    """
    freed = 0
    for index, message in enumerate(messages):
        if message.role != "tool" or len(message.content) <= limit_chars:
            continue
        smaller = clip(message.content, limit_chars=limit_chars)
        freed += estimate(message.content) - estimate(smaller)
        messages[index] = Message("tool", smaller, tool_call_id=message.tool_call_id)
    return freed


def evict(messages: list[Message], *, need: int, keep_last: int = 1) -> int:
    """Drop whole messages off the front until ``need`` tokens are freed.

    A prefix, never a hole: an assistant turn and the tool results answering it go
    together, or the next request describes results for calls that were never made.
    """
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
