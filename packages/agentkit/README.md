# agentkit

A minimal, framework-free tool-using agent loop. No LangChain, no LangGraph — the
core is a loop that asks a model for a turn, runs the tools it requests through a
fail-closed permission gate, and feeds the results back until the model is done.

Depends only on `pydantic` and a `ChatModel` protocol you implement for your
provider. Standalone by design: it never imports its host application (enforced by
a test), so it can be extracted and published on its own.

## What it gives you

- **`Agent`** — the loop: call model → run tool calls → feed results back → stop
  when a turn has no tool calls. Concurrency-safe tools in a turn run in parallel;
  every step is an `AgentEvent` for logging.
- **`budget`** — what actually ends a run: the context window, not a turn count. The
  transcript is measured before every call; over the ceiling, over-long tool results
  are clipped, the oldest ones cleared (the recent ones kept, so the model still has
  working context), and whole messages evicted only as a last resort. A run that
  spends its token budget gets one final turn with the tools withdrawn, so it answers
  from what it found instead of falling silent. `max_turns` is only a runaway backstop.
- **`Tool` / `FunctionTool` / `@tool`** — typed actions (pydantic params) with a
  fail-closed permission gate: read-only tools `ALLOW`, mutating tools `ASK`.
- **`subagent_tool`** — delegate a task to a named sub-agent in an isolated context;
  only its final answer returns to the parent. Bounded depth; events forward up.
- **`ChatModel`** protocol + **`FakeModel`** (in `agentkit.testing`) for offline runs.

## Sketch

```python
from agentkit import Agent, tool
from pydantic import BaseModel


class GrepArgs(BaseModel):
    pattern: str


@tool(read_only=True)
async def grep(args: GrepArgs) -> str:
    "Search the corpus."
    ...


agent = Agent(model=my_chat_model, tools=[grep], system="You are a librarian.")
result = await agent.run("Where is the tariff defined?")
print(result.text)
```

Provide `my_chat_model` by implementing `ChatModel.complete(system, messages, tools)`
against your provider (e.g. LiteLLM). See `agentkit.testing.FakeModel` for the shape.
