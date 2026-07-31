"""Tools: a typed action the model can invoke, with a fail-closed permission gate."""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, get_type_hints, runtime_checkable

from pydantic import BaseModel

from agentkit.messages import ToolSpec


class Permission(enum.Enum):
    """What may happen to a tool call before it runs."""

    ALLOW = "allow"  # run it
    ASK = "ask"  # run only if the host approves (see Agent.on_ask)
    DENY = "deny"  # never run


@runtime_checkable
class Tool(Protocol):
    """A named, typed action. Implement this or use :func:`tool` / :class:`FunctionTool`."""

    name: str
    description: str
    params: type[BaseModel]
    concurrency_safe: bool  # may run alongside other calls in the same turn

    def permission(self, args: BaseModel) -> Permission: ...

    async def run(self, args: BaseModel) -> str: ...


def tool_spec(t: Tool) -> ToolSpec:
    """The model-facing spec for a tool, with params as JSON Schema."""
    return ToolSpec(name=t.name, description=t.description, parameters=t.params.model_json_schema())


class FunctionTool:
    """A tool backed by an async function taking one pydantic model and returning text.

    Permission defaults fail-closed: a ``read_only`` tool is ALLOW, anything that
    mutates is ASK, unless an explicit ``permission`` callable overrides.
    """

    def __init__(
        self,
        *,
        name: str,
        description: str,
        params: type[BaseModel],
        handler: Callable[[Any], Awaitable[str]],
        read_only: bool = True,
        concurrency_safe: bool | None = None,
        permission: Callable[[BaseModel], Permission] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.params = params
        self.read_only = read_only
        # Read-only tools are safe to run in parallel unless told otherwise.
        self.concurrency_safe = read_only if concurrency_safe is None else concurrency_safe
        self._handler = handler
        self._permission = permission

    def permission(self, args: BaseModel) -> Permission:
        if self._permission is not None:
            return self._permission(args)
        return Permission.ALLOW if self.read_only else Permission.ASK

    async def run(self, args: BaseModel) -> str:
        return await self._handler(args)


def tool(
    *,
    name: str | None = None,
    description: str | None = None,
    read_only: bool = True,
    concurrency_safe: bool | None = None,
    permission: Callable[[BaseModel], Permission] | None = None,
) -> Callable[[Callable[[Any], Awaitable[str]]], FunctionTool]:
    """Wrap an ``async def fn(args: SomeModel) -> str`` into a :class:`FunctionTool`.

    The params model is read from the function's single argument annotation; the
    name and description default to the function's name and docstring.
    """

    def decorate(fn: Callable[[Any], Awaitable[str]]) -> FunctionTool:
        params = _single_model_param(fn)
        return FunctionTool(
            name=name or fn.__name__,
            description=description or (fn.__doc__ or "").strip(),
            params=params,
            handler=fn,
            read_only=read_only,
            concurrency_safe=concurrency_safe,
            permission=permission,
        )

    return decorate


def _single_model_param(fn: Callable[..., Any]) -> type[BaseModel]:
    """The pydantic model type of a handler's single argument."""
    hints = get_type_hints(fn)
    hints.pop("return", None)
    models = [t for t in hints.values() if isinstance(t, type) and issubclass(t, BaseModel)]
    if len(models) != 1:
        raise TypeError(
            f"{getattr(fn, '__name__', fn)} must take exactly one pydantic-model argument"
        )
    return models[0]
