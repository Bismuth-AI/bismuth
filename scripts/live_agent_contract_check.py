"""Run one bounded, non-mutating live-model check of the organizer family contract.

The check uses the configured provider and production organizer prompt/tool schemas. It
never reads or applies a user vault; all tree and card evidence below is synthetic.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import PurePosixPath

from agentkit import Agent, FunctionTool

from bismuth.adapters.llm.chat import LiteLLMChatModel
from bismuth.adapters.llm.litellm_adapter import close_clients
from bismuth.config import Settings
from bismuth.services.organizer.prompts import _LIBRARIAN_CONTEXT, SYSTEM_ORGANIZE
from bismuth.services.organizer.tools import _ArrivalsArgs, _SubmitPlanArgs, _TreeArgs

TREE = """\
./ (3 loose)
├── 금융/ (established policy-domain sibling)
└── 무역상업/ (established policy-domain sibling)
"""

ARRIVALS = """\
ID=D000001 | AT=금융/시행규칙 | TITLE=금융지원법 | TYPE=법률 | FAMILY=F001 | FAMILY_MEMBERS=D000001,D000002
ID=D000002 | AT=. | TITLE=금융지원법 시행령 | TYPE=대통령령 | FAMILY=F001 | FAMILY_MEMBERS=D000001,D000002
ID=D000003 | AT=. | TITLE=연구진흥법 | TYPE=법률 | FAMILY=F002 | FAMILY_MEMBERS=D000003,D000004
ID=D000004 | AT=. | TITLE=연구진흥법 시행령 | TYPE=대통령령 | FAMILY=F002 | FAMILY_MEMBERS=D000003,D000004
"""


async def main() -> None:
    settings = Settings()
    if not settings.is_configured:
        raise SystemExit("Bismuth model settings are not configured")

    submissions: list[dict[str, object]] = []
    rejections: list[list[str]] = []
    accepted: list[dict[str, object]] = []

    async def tree(_: _TreeArgs) -> str:
        return TREE

    async def arrivals(_: _ArrivalsArgs) -> str:
        return ARRIVALS

    async def submit_plan(args: _SubmitPlanArgs) -> str:
        payload = args.model_dump(mode="json")
        submissions.append(payload)
        problems: list[str] = []
        if len(args.boundaries) != 1:
            problems.append("submit exactly one root boundary")
        else:
            boundary = args.boundaries[0]
            if boundary.parent not in ("", "/", "."):
                problems.append("boundary parent must be root")
            if boundary.operation != "add_sibling":
                problems.append("the established root requires add_sibling")
            destinations = {
                document_id: move.target
                for move in boundary.moves
                for document_id in move.document_ids
            }
            if "D000001" in destinations:
                problems.append(
                    "D000001 is already below the final root shelf 금융; omit its no-op move"
                )
            if PurePosixPath(destinations.get("D000002", "")).name != "금융":
                problems.append(
                    "F001 must finish together: move D000002 to 금융; "
                    "D000001 already finishes there"
                )
            science_targets = {
                PurePosixPath(destinations.get(document_id, "")).name
                for document_id in ("D000003", "D000004")
            }
            if len(science_targets) != 1 or "" in science_targets:
                problems.append(
                    "F002 is indivisible: D000003,D000004 must share one new direct shelf"
                )
            elif science_targets & {"금융", "무역상업"}:
                problems.append("F002 needs a new policy-domain sibling")
        if problems:
            rejections.append(problems)
            return (
                "Plan rejected:\n- "
                + "\n- ".join(problems)
                + "\nRevise using the exact D-handles and submit once more."
            )
        accepted.append(payload)
        return "Shadow plan accepted: 1 boundary, 3 document moves."

    model = LiteLLMChatModel(
        model=settings.model_for(),
        api_key=settings.api_key,
        api_base=settings.api_base,
        timeout=min(settings.llm_timeout_seconds, 45.0),
        absolute_timeout=min(settings.llm_absolute_timeout_seconds, 60.0),
        max_tokens=1_024,
        max_concurrency=1,
        headers=settings.api_headers,
        body=settings.api_body,
    )
    submit = FunctionTool(
        name="submit_plan",
        description=(
            "Submit one exact root add_sibling candidate. Grounded FAMILY members must "
            "finish in one direct shelf; omit no-op moves already below that shelf."
        ),
        params=_SubmitPlanArgs,
        handler=submit_plan,
    )
    agent = Agent(
        model=model,
        tools=[
            FunctionTool(
                name="tree", description="Read the synthetic tree.", params=_TreeArgs, handler=tree
            ),
            FunctionTool(
                name="arrivals",
                description="Read all synthetic arrivals including FAMILY markers.",
                params=_ArrivalsArgs,
                handler=arrivals,
            ),
            submit,
        ],
        system=SYSTEM_ORGANIZE,
        max_turns=8,
        conclusion_turns=3,
        conclusion_tools={"submit_plan"},
        conclusion_accepted=lambda call, content, kind: (
            kind == "tool_result"
            and call.name == "submit_plan"
            and content.startswith("Shadow plan accepted:")
        ),
        context_policy=_LIBRARIAN_CONTEXT,
    )
    try:
        result = await agent.run(
            "This is a bounded shadow contract check. Inspect tree and arrivals. The root "
            "already uses 정책분야 with 금융 and 무역상업. File the loose arrivals, add a "
            "new reusable sibling when justified, preserve FAMILY final-shelf cohesion, "
            "and finish only with submit_plan."
        )
    finally:
        await close_clients()

    tool_names = [
        str(event.data.get("name", "")) for event in result.events if event.kind == "tool_call"
    ]
    print(
        json.dumps(
            {
                "ok": bool(accepted),
                "stopped": result.stopped,
                "turns": result.turns,
                "tool_calls": tool_names,
                "submission_count": len(submissions),
                "rejections": rejections,
                "accepted_plan": accepted[-1] if accepted else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not accepted:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
