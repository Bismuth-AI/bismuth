"""The agent loop, its permission gate, and the library boundary."""

from __future__ import annotations

import asyncio
import pathlib

import pytest
from pydantic import BaseModel

import agentkit
from agentkit import Agent, FunctionTool, Permission, ToolRegistry, tool, tool_spec
from agentkit.testing import FakeModel, call, says


class EchoArgs(BaseModel):
    text: str


@tool()
async def echo(args: EchoArgs) -> str:
    """Echo text back."""
    return f"echo: {args.text}"


class PathArgs(BaseModel):
    path: str


def make_delete(recorder: list[str]) -> FunctionTool:
    async def _delete(args: PathArgs) -> str:
        recorder.append(args.path)
        return f"deleted {args.path}"

    return FunctionTool(
        name="delete",
        description="Delete a path.",
        params=PathArgs,
        handler=_delete,
        read_only=False,
    )


def _tool_messages(result: agentkit.RunResult) -> list[str]:
    return [m.content for m in result.messages if m.role == "tool"]


class TestLoop:
    async def test_runs_a_tool_then_finishes(self) -> None:
        model = FakeModel([says("", call("echo", {"text": "hi"})), says("done")])
        result = await Agent(model=model, tools=[echo], system="s").run("go")

        assert result.text == "done"
        assert result.stopped == "final"
        assert "echo: hi" in _tool_messages(result)[0]

    async def test_history_is_replayed_before_the_new_input(self) -> None:
        """A follow-up question is only answerable next to what was said before it."""
        seen: list[list[str]] = []

        def watch(system: str, messages, tools):  # type: ignore[no-untyped-def]
            seen.append([f"{m.role}:{m.content}" for m in messages])
            return says("second answer")

        model = FakeModel(handler=watch)
        before = [
            agentkit.Message("user", "first question"),
            agentkit.Message("assistant", "first answer"),
        ]
        result = await Agent(model=model, tools=[echo], system="s").run(
            "and the newest one?", history=before
        )

        assert seen[0] == [
            "user:first question",
            "assistant:first answer",
            "user:and the newest one?",
        ]
        assert result.messages[:2] == before, "the transcript grows, it is not replaced"

    async def test_no_history_starts_from_the_question(self) -> None:
        model = FakeModel([says("only answer")])
        result = await Agent(model=model, tools=[echo], system="s").run("go")

        assert [m.content for m in result.messages] == ["go", "only answer"]

    async def test_max_turns_guard_stops_a_looping_model(self) -> None:
        # A model that always asks for a tool would loop forever without the guard.
        model = FakeModel(handler=lambda *_: says("again", call("echo", {"text": "x"})))
        result = await Agent(model=model, tools=[echo], system="s", max_turns=3).run("go")

        assert result.stopped == "max_turns"
        assert result.turns == 3


class TestDispatch:
    async def test_unknown_tool_is_reported_not_crashed(self) -> None:
        model = FakeModel([says("", call("nope", {})), says("ok")])
        result = await Agent(model=model, tools=[echo], system="s").run("go")

        assert "no tool named 'nope'" in _tool_messages(result)[0]
        assert result.text == "ok"

    async def test_bad_arguments_are_reported(self) -> None:
        model = FakeModel([says("", call("echo", {"wrong": "x"})), says("ok")])
        result = await Agent(model=model, tools=[echo], system="s").run("go")

        assert "invalid arguments" in _tool_messages(result)[0]

    async def test_a_tool_raising_does_not_crash_the_loop(self) -> None:
        class NoArgs(BaseModel):
            pass

        async def boom(_: NoArgs) -> str:
            raise RuntimeError("kaboom")

        boom_tool = FunctionTool(name="boom", description="", params=NoArgs, handler=boom)
        model = FakeModel([says("", call("boom", {})), says("ok")])
        result = await Agent(model=model, tools=[boom_tool], system="s").run("go")

        assert "Error running 'boom': kaboom" in _tool_messages(result)[0]
        assert result.text == "ok"


class TestPermission:
    async def test_mutating_tool_is_denied_without_approval(self) -> None:
        recorder: list[str] = []
        model = FakeModel([says("", call("delete", {"path": "a"})), says("ok")])
        result = await Agent(model=model, tools=[make_delete(recorder)], system="s").run("go")

        assert recorder == []  # never ran
        assert "needs user approval" in _tool_messages(result)[0]

    async def test_mutating_tool_runs_when_the_host_approves(self) -> None:
        recorder: list[str] = []

        async def approve(_tool: object, _args: object) -> Permission:
            return Permission.ALLOW

        model = FakeModel([says("", call("delete", {"path": "a"})), says("ok")])
        agent = Agent(model=model, tools=[make_delete(recorder)], system="s", on_ask=approve)
        result = await agent.run("go")

        assert recorder == ["a"]
        assert "deleted a" in _tool_messages(result)[0]


class TestObservability:
    async def test_events_trace_the_run(self) -> None:
        seen: list[agentkit.AgentEvent] = []
        model = FakeModel([says("", call("echo", {"text": "hi"})), says("done")])
        result = await Agent(model=model, tools=[echo], system="s", on_event=seen.append).run("go")

        kinds = [e.kind for e in result.events]
        assert kinds[:1] == ["turn"]
        for expected in ("tool_call", "tool_result", "stop"):
            assert expected in kinds
        assert seen == result.events  # callback and result agree


class TestToolPlumbing:
    def test_decorator_infers_name_description_and_params(self) -> None:
        assert echo.name == "echo"
        assert echo.description == "Echo text back."
        assert echo.params is EchoArgs

    def test_decorator_requires_one_model_param(self) -> None:
        with pytest.raises(TypeError):

            @tool()
            async def bad(x: int) -> str:  # not a pydantic model
                return str(x)

    def test_registry_rejects_duplicate_names(self) -> None:
        with pytest.raises(ValueError, match="duplicate tool name"):
            ToolRegistry([echo, echo])

    def test_spec_exposes_params_as_json_schema(self) -> None:
        spec = tool_spec(echo)
        assert spec.name == "echo"
        assert "text" in spec.parameters["properties"]


class TestSubAgent:
    async def test_task_delegates_and_returns_only_the_final_text(self) -> None:
        from agentkit import subagent_tool

        finder = Agent(
            model=FakeModel([says("found: the answer is 42")]), tools=[echo], system="sub"
        )
        task = subagent_tool({"finder": finder})
        main_model = FakeModel(
            [
                says("", call("task", {"description": "find it", "subagent_type": "finder"})),
                says("the sub-agent found 42"),
            ]
        )
        result = await Agent(model=main_model, tools=[task], system="main").run("go")

        tool_out = next(m.content for m in result.messages if m.role == "tool")
        assert tool_out == "found: the answer is 42"  # only the sub-agent's final text
        assert result.text == "the sub-agent found 42"

    async def test_task_reports_an_unknown_subagent(self) -> None:
        from agentkit import subagent_tool

        finder = Agent(model=FakeModel([says("x")]), tools=[echo], system="sub")
        task = subagent_tool({"finder": finder})
        model = FakeModel(
            [says("", call("task", {"description": "d", "subagent_type": "nope"})), says("ok")]
        )
        result = await Agent(model=model, tools=[task], system="main").run("go")

        assert "No sub-agent 'nope'" in _tool_messages(result)[0]

    async def test_delegation_depth_is_bounded(self) -> None:
        from agentkit.subagent import _depth, subagent_tool

        sub = Agent(model=FakeModel([says("should not run")]), tools=[echo], system="sub")
        task = subagent_tool({"finder": sub}, max_depth=2)
        model = FakeModel(
            [says("", call("task", {"description": "d", "subagent_type": "finder"})), says("ok")]
        )
        token = _depth.set(2)  # pretend we are already two levels deep
        try:
            result = await Agent(model=model, tools=[task], system="main").run("go")
        finally:
            _depth.reset(token)

        assert "depth limit" in _tool_messages(result)[0]

    async def test_subagent_events_forward_to_the_parent_sink(self) -> None:
        from agentkit import subagent_tool

        seen: list[agentkit.AgentEvent] = []
        sub = Agent(
            model=FakeModel([says("", call("echo", {"text": "hi"})), says("sub done")]),
            tools=[echo],
            system="sub",
        )
        task = subagent_tool({"finder": sub}, on_event=seen.append)
        main = Agent(
            model=FakeModel(
                [
                    says("", call("task", {"description": "go", "subagent_type": "finder"})),
                    says("ok"),
                ]
            ),
            tools=[task],
            system="main",
            on_event=seen.append,
        )

        await main.run("q")

        forwarded = [e for e in seen if e.kind.startswith("sub:")]
        assert forwarded  # the sub-agent's events reached the parent's sink
        assert all(e.data.get("subagent") == "finder" for e in forwarded)


class TestConcurrency:
    async def test_concurrency_safe_tools_in_one_turn_run_together(self) -> None:
        # A barrier only releases when both tool calls are in flight at once, so
        # this passes iff the two read-only tools ran in parallel.
        barrier = asyncio.Barrier(2)

        class NoArgs(BaseModel):
            pass

        async def wait_at_barrier(_: NoArgs) -> str:
            await asyncio.wait_for(barrier.wait(), 1.0)
            return "ok"

        t1 = FunctionTool(name="t1", description="", params=NoArgs, handler=wait_at_barrier)
        t2 = FunctionTool(name="t2", description="", params=NoArgs, handler=wait_at_barrier)
        model = FakeModel(
            [says("", call("t1", call_id="a"), call("t2", call_id="b")), says("done")]
        )

        result = await Agent(model=model, tools=[t1, t2], system="s").run("go")

        assert _tool_messages(result) == ["ok", "ok"]


def test_agentkit_never_imports_its_host() -> None:
    root = pathlib.Path(agentkit.__file__).parent
    for path in root.rglob("*.py"):
        assert "bismuth" not in path.read_text(encoding="utf-8"), f"{path} imports the host"
