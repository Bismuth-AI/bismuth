"""File one document by walking existing direct-child signs."""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from agentkit import Agent, FunctionTool
from agentkit.model import ChatModel

from bismuth.domain.document import DocumentCard
from bismuth.domain.paths import sanitize_segment
from bismuth.domain.placement import Placement, Verdict
from bismuth.logging_setup import log_trace
from bismuth.ports.catalog import Catalog
from bismuth.ports.llm import LLM
from bismuth.ports.vault import INBOX, Vault
from bismuth.prompts import placement as placement_prompts
from bismuth.services.families import grounded_family_keys, key_sets_overlap
from bismuth.services.sidecar import read_sidecar_meta

if TYPE_CHECKING:
    from bismuth.services.charters import CharterService

logger = logging.getLogger(__name__)

ROOT = PurePosixPath()


class PlacementService:
    """File one document with bounded agentic evidence and a root-safe fallback."""

    def __init__(
        self,
        llm: LLM,
        *,
        model: ChatModel | None = None,
        vault: Vault | None = None,
        catalog: Catalog | None = None,
        charters: CharterService | None = None,
    ) -> None:
        self._llm = llm
        self._model = model
        self._vault = vault
        self._catalog = catalog
        self._charters = charters

    async def decide(
        self,
        *,
        document_id: str,
        card: DocumentCard,
        folders: list[tuple[str, str]],
        existing_paths: frozenset[str],
    ) -> Placement:
        """Place one document against the current tree.

        ``existing_paths`` remains an explicit trust boundary supplied by the caller.
        Folder views not present in it are ignored even if an adapter returned them.
        """
        if all(
            dependency is not None
            for dependency in (self._model, self._vault, self._catalog, self._charters)
        ):
            placed = await self._decide_agentically(
                document_id=document_id,
                card=card,
                folders=folders,
                existing_paths=existing_paths,
            )
            if placed is not None:
                return placed
            log_trace(
                "place.agent_fallback",
                document_id=document_id,
                reason="agent ended without a valid finish_placement call",
            )
            # An agent that inspected the tree but never concluded has supplied no
            # positive evidence for any shelf.  Re-asking a weaker one-shot chooser can
            # turn that uncertainty into a forced fit, so preserve the document at root.
            return Placement(
                document_id=document_id,
                verdict=Verdict.PLACED,
                target=ROOT,
                created_folder=False,
                rationale="agent supplied no validated destination; kept at root",
            )

        return await self._decide_by_choices(
            document_id=document_id,
            card=card,
            folders=folders,
            existing_paths=existing_paths,
        )

    async def _decide_by_choices(
        self,
        *,
        document_id: str,
        card: DocumentCard,
        folders: list[tuple[str, str]],
        existing_paths: frozenset[str],
    ) -> Placement:
        """Compatibility circuit breaker: walk existing signs without creating paths."""
        purposes: dict[PurePosixPath, str] = {}
        for raw_path, purpose in folders:
            path = _safe_path(raw_path)
            if path is None or str(path) not in existing_paths:
                continue
            purposes[path] = purpose

        current = ROOT
        steps: list[dict[str, object]] = []
        asked_once = False
        while True:
            direct = sorted(
                ((path, purpose) for path, purpose in purposes.items() if path.parent == current),
                key=lambda item: (item[0].name.casefold(), str(item[0]).casefold()),
            )
            # Once a chosen child is a leaf, there is no decision left to outsource.
            if asked_once and not direct:
                break
            handles = {f"F{index:03d}": path for index, (path, _) in enumerate(direct, start=1)}
            prompt_children = [
                (folder_id, path.name, purpose)
                for folder_id, (path, purpose) in zip(handles, direct, strict=True)
            ]
            offered = [*handles, "STAY", "UNREADABLE"]
            raw_choice = await self._llm.choose(
                placement_prompts.build(
                    current=str(current),
                    children=prompt_children,
                    title=card.title,
                    doc_type=card.doc_type,
                    topics=list(card.topics),
                    summary=card.summary,
                    entities=[entity.name for entity in card.entities],
                ),
                choices=offered,
                max_tokens=32,
                temperature=0.0,
            )
            asked_once = True
            if raw_choice == "UNREADABLE":
                log_trace(
                    "place.decided",
                    document_id=document_id,
                    title=card.title,
                    current=str(current),
                    folders_offered=len(direct),
                    chose=None,
                    verdict=Verdict.INBOX.value,
                )
                return Placement.to_inbox(
                    document_id,
                    reason="document could not be read",
                )

            choice = raw_choice.strip().upper()
            if choice == "STAY":
                steps.append({"at": str(current), "choice": "stay"})
                break
            target = handles.get(choice)
            if target is None:
                # An unknown handle cannot name or escape to any folder. Staying at the
                # current level is the safe interpretation.
                log_trace(
                    "place.invalid_handle",
                    document_id=document_id,
                    current=str(current),
                    asked_for=raw_choice,
                    offered=list(handles),
                )
                steps.append({"at": str(current), "choice": "invalid"})
                break
            steps.append({"at": str(current), "choice": choice, "to": str(target)})
            current = target

        log_trace(
            "place.decided",
            document_id=document_id,
            title=card.title,
            folders_offered=len(purposes),
            chose=str(current),
            root=not current.parts,
            created_folder=False,
            steps=steps,
        )
        return Placement(
            document_id=document_id,
            verdict=Verdict.PLACED,
            target=current,
            created_folder=False,
            rationale="selected through existing folder signs" if current.parts else "kept here",
        )

    async def _decide_agentically(
        self,
        *,
        document_id: str,
        card: DocumentCard,
        folders: list[tuple[str, str]],
        existing_paths: frozenset[str],
    ) -> Placement | None:
        """Let a tool-using agent inspect only evidence addressable for this document."""

        assert self._model is not None
        assert self._vault is not None
        assert self._catalog is not None
        assert self._charters is not None

        purposes: dict[PurePosixPath, str] = {ROOT: "vault root"}
        for raw_path, purpose in folders:
            path = _safe_path(raw_path)
            if path is None or str(path) not in existing_paths:
                continue
            purposes[path] = purpose
        ordered_folders = [ROOT, *sorted((p for p in purposes if p.parts), key=str)]
        folder_handles = {
            ("FROOT" if not path.parts else f"F{index:04d}"): path
            for index, path in enumerate(ordered_folders)
        }
        folder_id_by_path = {path: handle for handle, path in folder_handles.items()}

        document_paths = self._document_paths()
        inspected_folders: set[str] = set()

        async def inspect_folder(args: placement_prompts.InspectFolderArgs) -> str:
            handle = args.folder_id.strip().upper()
            if handle in inspected_folders:
                return f"Folder {handle} was already inspected; use the evidence already supplied."
            path = folder_handles.get(handle)
            if path is None:
                return f"Unknown folder ID: {args.folder_id}"
            inspected_folders.add(handle)
            children = [
                (folder_id_by_path[child], child.name, purposes.get(child, ""))
                for child in ordered_folders
                if child.parts and child.parent == path
            ]
            direct_cards = [
                (doc_id, self._catalog.load_card(doc_id))
                for doc_id, doc_path in document_paths.items()
                if doc_path.parent == path
            ]
            lines = [
                f"FOLDER={handle} PATH={path or PurePosixPath('.')} SIGN={purposes.get(path, '')}",
                "CHILDREN:",
                *(
                    f"  [{child_id}] {name} — {purpose or '(no routing sign)'}"
                    for child_id, name, purpose in children
                ),
                "DIRECT DOCUMENTS:",
            ]
            for _, existing_card in direct_cards[:8]:
                if existing_card is None:
                    continue
                lines.append(
                    f"  - {existing_card.title} | "
                    f"{', '.join(existing_card.topics)} | {existing_card.summary}"
                )
            if not children:
                lines.append("  (no child folders)")
            if not direct_cards:
                lines.append("  (no direct documents)")
            return "\n".join(lines)

        decisions: list[Placement] = []

        async def finish_placement(args: placement_prompts.FinishPlacementArgs) -> str:
            handle = args.folder_id.strip().upper()
            target = folder_handles.get(handle)
            if target is None:
                return f"Rejected: unknown folder ID {args.folder_id!r}."
            decisions.append(
                Placement(
                    document_id=document_id,
                    verdict=Verdict.PLACED,
                    target=target,
                    rationale=(
                        "agent selected an existing shelf"
                        if target.parts
                        else "agent kept the document at root"
                    ),
                )
            )
            return f"Accepted existing destination: {target or '/'}"

        tools = [
            FunctionTool(
                name="inspect_folder",
                description=(
                    "Read one shown folder's routing sign, direct children and a bounded "
                    "sample of documents sitting directly there."
                ),
                params=placement_prompts.InspectFolderArgs,
                handler=inspect_folder,
                read_only=True,
            ),
            FunctionTool(
                name="finish_placement",
                description=(
                    "Submit the one validated filing action against the existing tree. "
                    "This is terminal after the host accepts the folder handle."
                ),
                params=placement_prompts.FinishPlacementArgs,
                handler=finish_placement,
                read_only=True,
            ),
        ]

        def emit(event: object) -> None:
            kind = getattr(event, "kind", "event")
            data = dict(getattr(event, "data", {}))
            log_trace(f"place.agent_{kind}", document_id=document_id, **data)

        agent = Agent(
            model=self._model,
            tools=tools,
            system=placement_prompts.AGENT_SYSTEM,
            # Each folder can be inspected once, followed by a terminal decision.  Folder
            # inspection already includes representative direct documents; exposing the
            # same cards through a second tool caused Qwen to enumerate them indefinitely.
            max_turns=len(folder_handles) + 2,
            conclusion_tools={"finish_placement"},
            conclusion_accepted=lambda *_: bool(decisions),
            require_conclusion_tool=True,
            tool_choice="required",
            on_event=emit,
        )
        await agent.run(
            placement_prompts.build_agent(
                title=card.title,
                doc_type=card.doc_type,
                topics=list(card.topics),
                summary=card.summary,
                entities=[entity.name for entity in card.entities],
                folders=[
                    (folder_id_by_path[path], str(path), purposes.get(path, ""))
                    for path in ordered_folders
                ],
                related=[],
            )
        )
        if not decisions:
            return None
        placement = decisions[-1]
        if placement.target is not None and placement.target.parts:
            try:
                examples = []
                example_family_keys: list[set[str]] = []
                for existing_id, existing_path in document_paths.items():
                    if existing_path.parent != placement.target:
                        continue
                    existing_card = self._catalog.load_card(existing_id)
                    if existing_card is not None:
                        keys = grounded_family_keys(existing_card, existing_path.name)
                        # Revisions and subordinate instruments of one law are useful
                        # membership evidence, but showing several of them as separate
                        # representatives makes a broad shelf look artificially narrow.
                        # Keep at most one representative per grounded source family.
                        if keys and any(
                            key_sets_overlap(keys, seen) for seen in example_family_keys
                        ):
                            continue
                        examples.append(
                            (existing_card.title, ", ".join(existing_card.topics[:8]))
                        )
                        if keys:
                            example_family_keys.append(keys)
                    if len(examples) >= 5:
                        break
                audit = await self._llm.choose(
                    placement_prompts.build_fit_audit(
                        title=card.title,
                        doc_type=card.doc_type,
                        topics=list(card.topics),
                        summary=card.summary,
                        path=str(placement.target),
                        sign=purposes.get(placement.target, ""),
                        examples=examples,
                        alternatives=[
                            (str(path), purpose)
                            for path, purpose in purposes.items()
                            if path.parts and path != placement.target
                        ],
                    ),
                    choices=("SHELF", "STAY"),
                    max_tokens=8,
                )
            except Exception as exc:
                audit = "STAY"
                log_trace(
                    "place.agent_audit_failed",
                    document_id=document_id,
                    proposed=str(placement.target),
                    error=f"{type(exc).__name__}: {exc}",
                )
            log_trace(
                "place.agent_audit",
                document_id=document_id,
                proposed=str(placement.target),
                verdict=audit,
            )
            if audit != "SHELF":
                placement = Placement(
                    document_id=document_id,
                    verdict=Verdict.PLACED,
                    target=ROOT,
                    rationale="agent destination failed the positive-fit audit",
                )
        log_trace(
            "place.decided",
            document_id=document_id,
            title=card.title,
            chose=str(placement.target or ""),
            root=placement.target == ROOT,
            created_folder=placement.created_folder,
            mode="incremental_agent",
        )
        return placement

    def _document_paths(self) -> dict[str, PurePosixPath]:
        assert self._vault is not None
        result: dict[str, PurePosixPath] = {}
        for path in self._vault.iter_files(ROOT, recursive=True):
            if path.parts and path.parts[0] == INBOX.parts[0]:
                continue
            sidecar = path.parent / f"{path.name}.md"
            if not self._vault.exists(sidecar):
                continue
            meta = read_sidecar_meta(self._vault.read_text(sidecar)) or {}
            document_id = str(meta.get("document_id", ""))
            if document_id:
                result[document_id] = path
        return result

    def _related_cards(
        self,
        card: DocumentCard,
        *,
        document_paths: dict[str, PurePosixPath],
    ) -> list[tuple[str, DocumentCard, PurePosixPath]]:
        assert self._catalog is not None
        needle = _semantic_terms(card)
        scored: list[tuple[float, str, DocumentCard, PurePosixPath]] = []
        for document_id, existing in self._catalog.iter_cards():
            path = document_paths.get(document_id)
            if path is None:
                continue
            score = _overlap_score(needle, _semantic_terms(existing))
            if score > 0:
                scored.append((score, document_id, existing, path))
        scored.sort(key=lambda row: (-row[0], row[2].title.casefold(), row[1]))
        return [(document_id, existing, path) for _, document_id, existing, path in scored[:12]]


def _safe_path(raw: str) -> PurePosixPath | None:
    """Turn an adapter path into a vault-safe relative path, or ``None``."""
    segments: list[str] = []
    for part in raw.replace("\\", "/").split("/"):
        part = part.strip()
        if not part or part in (".", ".."):
            continue
        try:
            segments.append(sanitize_segment(part))
        except ValueError:
            continue
    return PurePosixPath(*segments) if segments else None


def _semantic_terms(card: DocumentCard) -> set[str]:
    """Generic retrieval terms only; they shortlist evidence and never decide a shelf."""

    values = [
        card.title,
        card.doc_type,
        *card.topics,
        *card.keywords,
        *(entity.name for entity in card.entities),
    ]
    terms: set[str] = set()
    for value in values:
        normalized = "".join(character.casefold() for character in value if character.isalnum())
        if len(normalized) >= 2:
            terms.add(normalized)
            terms.update(normalized[index : index + 2] for index in range(len(normalized) - 1))
        terms.update(part.casefold() for part in value.split() if len(part) >= 2)
    return terms


def _overlap_score(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    shared = left & right
    if not shared:
        return 0.0
    return len(shared) / max(1, min(len(left), len(right)))
