"""Structured, validated local subdivision service used after document placement."""

# ruff: noqa: E402, F401 -- service mixins share the legacy dependency surface


from __future__ import annotations

import asyncio
import logging
import math
import re
import unicodedata
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TypeVar

from bismuth.domain.charter import CHARTER_FILENAME, Charter, boundary_purpose, routing_purpose
from bismuth.domain.document import DocumentCard, sidecar_name
from bismuth.domain.errors import BismuthError
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.domain.maintenance import ProposedClass, normalise_label, validate_plan
from bismuth.domain.paths import sanitize_segment
from bismuth.domain.progress import Progress, ProgressSink, Stage, report
from bismuth.logging_setup import log_trace
from bismuth.ports.catalog import Catalog
from bismuth.ports.llm import LLM, Prompt
from bismuth.ports.vault import INBOX, STATE_DIR, Vault
from bismuth.prompts import subdivision as prompts
from bismuth.services.charters import CharterService
from bismuth.services.families import family_text, grounded_family_keys, key_sets_overlap
from bismuth.services.sidecar import read_sidecar_meta
from bismuth.services.transactor import Transactor

logger = logging.getLogger(__name__)

# Character budgeting is deliberately provider-neutral.  Tokenizers differ, but a
# 32k-character ceiling leaves a wide margin inside the smallest supported 65k-token
# context even with schema/tool framing.  Every maintenance call is built and measured
# before it reaches the adapter.
MAX_MAINTENANCE_PROMPT_CHARS = 32_000
PacketT = TypeVar("PacketT")
from bismuth.services.legacy.subdivision.application import SubdivisionApplicationMixin
from bismuth.services.legacy.subdivision.evaluation import SubdivisionEvaluationMixin
from bismuth.services.legacy.subdivision.helpers import (
    _boundary_wording_problem,
    _bounded_gather,
    _describe,
    _document_packets,
    _emerging_packets,
    _failed_boundary_checks,
    _failed_routing_checks,
    _free_filename,
    _groups_for_ids,
    _groups_relevant_to_ids,
    _in_inbox,
    _normalise,
    _normalise_sketch,
    _prompt_chars,
    _relevant_children,
    _same_axis,
    _same_name,
    _sketch_packets,
    _value_packets,
    _writing_system,
)
from bismuth.services.legacy.subdivision.models import Divided, _Contents

_GENERIC_ROOT_SIGNS = frozenset(
    normalise_label(name)
    for name in (
        "산업",
        "경제",
        "정책",
        "규제",
        "법률",
        "사업",
        "기업",
        "시장",
        "행정",
        "공공 행정",
        "공공행정",
        "행정학",
        "법령 일반",
        "일반",
        "기타",
        "industry",
        "economy",
        "policy",
        "regulation",
        "law",
        "business",
        "administration",
        "administrative",
        "public administration",
        "administrative studies",
        "general law",
        "general",
        "other",
    )
)


def _root_normalization_is_grounded(original: str, normalized: str) -> bool:
    """A generated root label must retain a meaningful lexical anchor from its source."""

    left = normalise_label(original)
    right = normalise_label(normalized)
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    tokens = {
        token
        for raw in re.findall(r"[가-힣]{2,}|[a-z0-9]{3,}", original.casefold())
        if (token := normalise_label(raw))
    }
    matches = {token for token in tokens if token in right}
    if len(matches) >= 2:
        return True
    # One short word occurring inside a long, unrelated label is not grounding.  It
    # previously accepted 산업/기술 지원 -> 과학기술정보통신부 소관 법령 merely because
    # both strings contain 기술.  A single anchor is sufficient only when it explains
    # a substantial part of the proposed normalized sign (기업 -> 중소기업, for example).
    return any(len(token) / len(right) >= 0.4 for token in matches)


class LibraryMaintenanceService(SubdivisionEvaluationMixin, SubdivisionApplicationMixin):
    """Maintains the classification tree as evidence arrives.

    Placement shelves one document against the tree that exists.  This service owns
    changes to that tree: adding a class and reviewing or replacing an old boundary.
    Keeping the use cases separate makes a maintenance failure independent from a
    successfully filed document.
    """

    # Placement already asks the per-document agent whether a new arrival belongs in
    # an existing shelf.  A document left loose is therefore an explicit "none of the
    # above" decision, not an unprocessed backlog.  The old batch router asked the same
    # question again over the entire loose pile and could overwrite that decision with
    # a forced nearest-fit assignment.  Maintenance may still discover a genuinely new
    # sibling from the loose pile, but it must not silently re-file it into old signs.
    _BULK_ROUTE_EXPLICITLY_LOOSE_DOCUMENTS = False

    def __init__(
        self,
        *,
        vault: Vault,
        catalog: Catalog,
        charters: CharterService,
        transactor: Transactor,
        llm: LLM,
    ) -> None:
        self._vault = vault
        self._catalog = catalog
        self._charters = charters
        self._transactor = transactor
        self._llm = llm
        # A declined first boundary is a judgement over the evidence seen at that
        # size. Repeating it for every subsequent arrival produced thousands of
        # near-identical calls without adding information. This is process-local on
        # purpose: restarting forgets no durable classification state, while a live
        # ingest run avoids retrying until the loose pile has materially grown.
        self._initial_boundary_attempt_at: dict[PurePosixPath, int] = {}
        # A semantically invalid candidate is negative evidence too. Without carrying
        # it into the next materially-grown pass, Qwen repeatedly proposed the same
        # one-document "산업 안전" class while obvious unrelated clusters accumulated.
        # The exclusion expires after the loose evidence doubles, so a genuinely grown
        # class can be reconsidered later rather than being permanently blacklisted.
        self._recent_emerging_candidates: dict[PurePosixPath, dict[str, tuple[str, int]]] = {}

    def _recent_candidates(self, folder: PurePosixPath, documents: int) -> list[str]:
        remembered = self._recent_emerging_candidates.get(folder, {})
        return sorted(
            original
            for original, seen_at in remembered.values()
            if documents < max(seen_at + 4, seen_at * 2)
        )

    def _remember_candidate(
        self, folder: PurePosixPath, name: str, *, documents: int
    ) -> None:
        normalized = normalise_label(name)
        if not normalized:
            return
        self._recent_emerging_candidates.setdefault(folder, {})[normalized] = (
            name.strip(),
            documents,
        )

    def _cohere_families(
        self, contents: _Contents, groups: list[prompts.Group]
    ) -> list[prompts.Group] | None:
        """Keep every colocated document family in one proposed destination."""

        copies = [group.model_copy(deep=True) for group in groups]
        for family in self._document_families(contents):
            # None is a real destination: it means the document remains at the
            # parent. A partial family proposal is rejected rather than silently
            # rewritten into a semantically false shelf.
            destinations = {
                next(
                    (
                        index
                        for index, group in enumerate(copies)
                        if document_id in group.document_ids
                    ),
                    None,
                )
                for document_id in family
            }
            if len(destinations) > 1:
                return None
        return copies

    def _document_families(self, contents: _Contents) -> list[set[str]]:
        """Resolve request-local handles through their paths before comparing cards."""

        keyed: list[tuple[str, set[str]]] = []
        for document_id, _, path in contents.documents:
            card = self._card_of(path)
            if card is None:
                continue
            keys = grounded_family_keys(card, path.name)
            if keys:
                keyed.append((document_id, keys))

        families: list[set[str]] = []
        family_keys: list[set[str]] = []
        for document_id, keys in keyed:
            matching = [
                index for index, known in enumerate(family_keys) if key_sets_overlap(keys, known)
            ]
            if not matching:
                families.append({document_id})
                family_keys.append(set(keys))
                continue
            first = matching[0]
            families[first].add(document_id)
            family_keys[first].update(keys)
            for index in reversed(matching[1:]):
                families[first].update(families.pop(index))
                family_keys[first].update(family_keys.pop(index))

        return [members for members in families if len(members) > 1]

    def _independent_family_units(self, contents: _Contents, selected: set[str]) -> int:
        """Count evidence units, treating each grounded document family as one."""

        covered: set[str] = set()
        units = 0
        for family in self._document_families(contents):
            chosen = family & selected
            if not chosen:
                continue
            # _cohere_families has already rejected partial family assignments.
            covered.update(chosen)
            units += 1
        return units + len(selected - covered)

    def _annotate_family_units(self, contents: _Contents) -> None:
        """Expose grounded atomic families in the exact model-facing document rows."""

        annotations: dict[str, str] = {}
        ordered = sorted(self._document_families(contents), key=lambda members: sorted(members))
        for members in ordered:
            shown = ",".join(sorted(members))
            annotation = f"ATOMIC_MEMBERS={shown} | ASSIGN_TOGETHER"
            for document_id in members:
                annotations[document_id] = annotation
        contents.documents = [
            (
                document_id,
                f"{description} | {annotations[document_id]}"
                if document_id in annotations
                else description,
                path,
            )
            for document_id, description, path in contents.documents
        ]

    def _initial_boundary_lines(self, contents: _Contents) -> list[tuple[str, str]]:
        """Semantic evidence for choosing a folder axis, without metadata facets."""

        lines: list[tuple[str, str]] = []
        for document_id, description, path in contents.documents:
            card = self._card_of(path)
            if card is None:
                continue
            # Titles and summaries of formal documents repeat words such as "law",
            # "decree" and "rule" many more times than their actual subject.  Qwen
            # consequently selected legal form even when explicitly forbidden.  The
            # card already has a dedicated retrieval surface for subject classification;
            # use that surface here and keep identity/family constraints separate.
            identity_keys = grounded_family_keys(card, path.name)
            topics = [
                topic for topic in card.topics if family_text(topic) not in identity_keys
            ]
            parts: list[str] = []
            if topics:
                parts.append("SUBJECT_TOPICS=" + ", ".join(topics))
            if card.keywords:
                parts.append("KEYWORDS=" + ", ".join(card.keywords))
            if not parts:
                parts.append("SUBJECT_TOPICS=(insufficient card evidence)")
            if "ATOMIC_MEMBERS=" in description:
                parts.append("ATOMIC_MEMBERS=" + description.split("ATOMIC_MEMBERS=", 1)[1])
            lines.append((document_id, " | ".join(parts)))
        return lines

    def _explicit_card_value_problem(
        self, contents: _Contents, groups: list[prompts.Group]
    ) -> str | None:
        """Keep a sign that is literally a card type honest about its membership.

        This does not prefer or ban a taxonomy. It activates only when the model chose
        a sign exactly equal to a value already present in the archive's ``doc_type``
        metadata. Such a sign cannot truthfully contain a document with another value.
        """

        cards = {
            document_id: card
            for document_id, _, path in contents.documents
            if (card := self._card_of(path)) is not None
        }
        type_values = {normalise_label(card.doc_type) for card in cards.values()}
        for group in groups:
            sign = normalise_label(group.name)
            if not sign or sign not in type_values:
                continue
            mismatches = [
                document_id
                for document_id in group.document_ids
                if (card := cards.get(document_id)) is not None
                and normalise_label(card.doc_type) != sign
            ]
            if mismatches:
                return (
                    f"sign {group.name!r} equals a card doc_type but contains "
                    f"different doc_type members: {','.join(mismatches)}"
                )

        # Model wording is not a dependable metadata detector: Qwen may call the
        # archive value ``대통령령`` a friendlier ``시행령`` or describe the same facet
        # as "legal effect".  Detect the behaviour from the proposed membership
        # instead.  If every evidenced sibling is homogeneous by card-authored type
        # and different siblings select different types, the proposed boundary is a
        # document-type partition regardless of how it was named.  Delaying a rare
        # coincidental split is safer than creating a metadata hierarchy that later
        # tears atomic document families apart.
        group_types = [
            {
                normalise_label(card.doc_type)
                for document_id in group.document_ids
                if (card := cards.get(document_id)) is not None and card.doc_type.strip()
            }
            for group in groups
            if len(group.document_ids) >= 2
        ]
        if (
            len(group_types) >= 2
            and all(len(values) == 1 for values in group_types)
            and len(set().union(*group_types)) >= 2
        ):
            return "proposed siblings partition documents by card doc_type metadata"
        return None

    def _sketch_uses_card_metadata_facet(
        self, contents: _Contents, sketch: prompts.ReplacementSketch
    ) -> bool:
        """Whether every proposed sign is merely a card-authored metadata value.

        The check is inferred from this archive rather than a Korean/English keyword
        list.  Besides the explicit ``doc_type`` field, the final token of a spaced
        title is useful because source titles often say ``... 시행령`` while the card
        records the formal type as ``대통령령``.  A semantic sign such as 금융소비자 or
        과학기술 will not make every sibling match these observed facet values.
        """

        card_by_id = {
            document_id: card
            for document_id, _, path in contents.documents
            if (card := self._card_of(path)) is not None
        }
        known_types = {
            value
            for card in card_by_id.values()
            for value in (
                normalise_label(card.doc_type),
                normalise_label(card.title.rsplit(maxsplit=1)[-1])
                if len(card.title.rsplit(maxsplit=1)) > 1
                else "",
            )
            if value
        }
        sign_values = {normalise_label(sign.name) for sign in sketch.signs}
        sign_parts = {
            normalise_label(item.name): {
                normalise_label(part)
                for part in re.findall(r"[^\W_]+", item.name, flags=re.UNICODE)
                if normalise_label(part)
            }
            for item in sketch.signs
        }

        def matches_metadata(sign: str) -> bool:
            candidates = {sign, *sign_parts.get(sign, set())}
            return any(
                candidate == value
                or candidate in value
                or value in candidate
                for candidate in candidates
                for value in known_types
            )

        return bool(sign_values) and all(
            matches_metadata(sign) for sign in sign_values
        )

    def _initial_boundary_due(self, folder: PurePosixPath, documents: int) -> bool:
        """Retry a rejected first boundary only after materially new evidence.

        This does not decide *when a folder should split* and is not a corpus batch
        threshold. The model still gets the first valid opportunity at three direct
        documents. It only prevents re-sending the same declined question at sizes
        n+1, n+2, ...; growth is at least four documents and roughly thirty-five percent.
        """

        previous = self._initial_boundary_attempt_at.get(folder, 0)
        if previous == 0 or documents == previous:
            return True
        # Four new loose documents are material contrast at any collection size. A
        # percentage gate grew to 16+ documents on a large root, so one malformed
        # candidate near the end of a batch suppressed every other visible cluster.
        # The expensive work is document carding; one short structure question per
        # four genuinely loose arrivals is both bounded and responsive.
        next_size = previous + 4
        if documents >= next_size:
            return True
        log_trace(
            "subdivide.skipped",
            folder=str(folder),
            reason="initial boundary evidence has not materially grown since rejection",
            documents=documents,
            previous_attempt=previous,
            next_attempt=next_size,
        )
        return False

    async def consider_with_ancestors(
        self,
        folder: PurePosixPath,
        *,
        filename: str = "",
        on_progress: ProgressSink | None = None,
    ) -> list[Divided]:
        """Consider the folder a document landed in, then every folder above it.

        Without the walk up, a top-level division is permanent. Once the root has
        children, documents land in the children and the root is never passed here
        again -- so the division it made when it held thirteen documents would still
        be its division at ten thousand. The ancestors are all divided already, so
        each is gated by the doubling rule and usually costs nothing.
        """
        results = await self.consider(folder, filename=filename, on_progress=on_progress)
        parent = folder.parent
        while folder.parts:
            # Nothing arrived directly in an ancestor, so its loose pile did not change.
            # Asking whether another class emerged there repeats the same question with
            # the same evidence.  The subtree did grow, so a due boundary review still
            # runs.  On the 300-document corpus this distinction removes hundreds of
            # redundant root calls without freezing the top-level axis.
            results.extend(
                await self.consider(
                    parent,
                    filename=filename,
                    on_progress=on_progress,
                    allow_emerging=False,
                )
            )
            folder = parent
            parent = folder.parent
        return results

    async def finalize_pending(
        self, *, focus_filenames: set[str] | None = None
    ) -> list[Divided]:
        """Evaluate the final unasked growth left by an upload selection.

        Retry throttling avoids near-identical calls after every arrival.  A selection
        can end before the next threshold, though; those final arrivals are real new
        evidence and must not wait for another upload.  Only folders whose first
        boundary was already attempted and whose loose evidence grew are revisited.
        """
        results: list[Divided] = []
        for folder, previous in list(self._initial_boundary_attempt_at.items()):
            if not folder.parts:
                continue
            current = len(self._read(folder).documents)
            if current <= previous:
                continue
            self._initial_boundary_attempt_at.pop(folder, None)
            results.extend(await self.consider(folder))

        # One judgement deliberately draws only one recurring class. If the document
        # that caused that class to be created is the final item in an upload selection,
        # there is no next arrival to ask about the freshly reduced loose root. The old
        # finalizer looked only at rejected-attempt state (which success clears), so a
        # 150-file batch could create one last shelf and strand fifty obvious documents.
        # Drain validated root classes one transaction at a time until a pass makes no
        # change. Every successful pass moves at least two independent families, making
        # the loop structurally finite without a model/tool-turn constant.
        root = PurePosixPath()
        route_attempts: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()
        self._initial_boundary_attempt_at.pop(root, None)
        while len(self._read(root).documents) >= 3:
            divided = await self.consider(root)
            results.extend(divided)
            if any(result.happened for result in divided):
                self._initial_boundary_attempt_at.pop(root, None)
                continue

            # Placement is intentionally fail-safe and may leave a clear member loose.
            # Recover only closed, independently verified positives after all arrivals:
            # one family chooses a shown sign, then every member must pass an isolated
            # membership audit. This is not the former packet router and cannot force
            # an unclaimed document into the closest existing shelf.
            routed = await self._route_verified_existing(root, attempted=route_attempts)
            if routed is None or not routed.happened:
                break
            results.append(routed)
            self._initial_boundary_attempt_at.pop(root, None)
        if focus_filenames:
            rebalanced = await self._rebalance_focus(focus_filenames)
            if rebalanced is not None and rebalanced.happened:
                results.append(rebalanced)
        return results

    async def _route_verified_existing(
        self,
        folder: PurePosixPath,
        *,
        attempted: set[tuple[tuple[str, ...], tuple[str, ...]]] | None = None,
    ) -> Divided | None:
        """Route loose atomic families only after two closed positive judgements."""

        contents = self._read(folder)
        self._annotate_family_units(contents)
        charter = self._charter(folder)
        if (
            charter is None
            or not charter.managed
            or not charter.divided
            or not charter.split_basis
            or not charter.split_question
            or not contents.children
            or not contents.documents
        ):
            return None

        descriptions = dict(contents.lines)
        families = self._document_families(contents)
        covered = set().union(*families) if families else set()
        singles = (
            {document_id}
            for document_id in descriptions
            if document_id not in covered
        )
        units = [*families, *singles]
        handles = {
            f"F{index:03d}": (name, note)
            for index, (name, note) in enumerate(contents.children, start=1)
        }
        topology = tuple(name for name, _ in contents.children)

        async def route_unit(unit: set[str]) -> tuple[str, tuple[str, ...]] | None:
            ordered = tuple(sorted(unit))
            fingerprint = tuple(
                sorted(
                    str(path)
                    for document_id in ordered
                    if (path := contents.path_of(document_id)) is not None
                )
            )
            attempt_key = (topology, fingerprint)
            if attempted is not None and attempt_key in attempted:
                return None
            if attempted is not None:
                attempted.add(attempt_key)
            evidence = " | ATOMIC FAMILY: ".join(descriptions[item] for item in ordered)
            choice = await self._llm.choose(
                prompts.build_existing_choice(
                    path=str(folder),
                    document=(ordered[0], evidence),
                    axis=charter.split_basis,
                    axis_question=charter.split_question,
                    children=contents.children,
                ),
                choices=(*handles, "NEW_SIBLING"),
                max_tokens=8,
                temperature=0.0,
            )
            if choice not in handles:
                return None
            name, _ = handles[choice]

            async def verify(document_id: str) -> str:
                return await self._llm.choose(
                    prompts.build_member_fit_audit(
                        path=str(folder),
                        axis=charter.split_basis,
                        axis_question=charter.split_question,
                        name=name,
                        document=(document_id, descriptions[document_id]),
                    ),
                    choices=("BELONG", "STAY"),
                    max_tokens=8,
                    temperature=0.0,
                )

            verdicts = await _bounded_gather(ordered, verify)
            log_trace(
                "subdivide.existing_fit_audit",
                folder=str(folder),
                sign=name,
                family=list(ordered),
                verdicts=verdicts,
            )
            if any(verdict != "BELONG" for verdict in verdicts):
                return None
            return choice, ordered

        decisions = await _bounded_gather(units, route_unit)
        grouped: dict[str, list[str]] = {}
        for decision in decisions:
            if decision is None:
                continue
            handle, document_ids = decision
            grouped.setdefault(handle, []).extend(document_ids)
        if not grouped:
            return None

        groups = [
            prompts.Group(
                name=handles[handle][0],
                note=handles[handle][1],
                document_ids=document_ids,
            )
            for handle, document_ids in grouped.items()
        ]
        plan = prompts.Division(
            basis=charter.split_basis,
            basis_question=charter.split_question,
            groups=groups,
            reuse_existing=True,
        )
        return self._route_existing(folder, contents, plan, charter)

    async def _rebalance_focus(self, focus_filenames: set[str]) -> Divided | None:
        """Conservatively correct this upload after all sibling signs are visible."""

        root = PurePosixPath()
        charter = self._charter(root)
        contents = self._read(root, recursive=True)
        direct_children = sorted(
            (item for item in contents.children if "/" not in item[0]),
            key=lambda item: item[0].casefold(),
        )
        if (
            charter is None
            or not charter.managed
            or not charter.divided
            or not charter.split_basis
            or not charter.split_question
            or len(direct_children) < 2
        ):
            return None

        contents.documents = [
            item
            for item in contents.documents
            if len(item[2].parts) == 2 and item[2].name in focus_filenames
        ]
        if not contents.documents:
            return None
        self._annotate_family_units(contents)
        descriptions = dict(contents.lines)
        families = self._document_families(contents)
        covered = set().union(*families) if families else set()
        singles = ({item} for item in descriptions if item not in covered)
        units = [*families, *singles]
        handles = {
            f"F{index:03d}": name
            for index, (name, _) in enumerate(direct_children, start=1)
        }
        handle_for_name = {name: handle for handle, name in handles.items()}

        async def review(
            unit: set[str],
        ) -> tuple[str, str, tuple[str, ...]] | None:
            ordered = tuple(sorted(unit))
            paths = [contents.path_of(item) for item in ordered]
            if any(path is None for path in paths):
                return None
            parents = {path.parent.name for path in paths if path is not None}
            if len(parents) != 1:
                return None
            current = next(iter(parents))
            current_handle = handle_for_name.get(current)
            if current_handle is None:
                return None
            evidence = " | ATOMIC FAMILY: ".join(descriptions[item] for item in ordered)
            choice = await self._llm.choose(
                prompts.build_rebalance_choice(
                    path=str(root),
                    current=current,
                    document=(ordered[0], evidence),
                    axis=charter.split_basis,
                    axis_question=charter.split_question,
                    children=direct_children,
                ),
                choices=(*handles, "KEEP"),
                max_tokens=8,
                temperature=0.0,
            )
            if choice not in handles or choice == current_handle:
                return None
            proposed = handles[choice]
            comparison = await self._llm.choose(
                prompts.build_rebalance_comparison(
                    current=current,
                    proposed=proposed,
                    document=(ordered[0], evidence),
                ),
                choices=("MOVE", "KEEP"),
                max_tokens=8,
                temperature=0.0,
            )
            if comparison != "MOVE":
                return None

            async def verify(document_id: str) -> str:
                return await self._llm.choose(
                    prompts.build_member_fit_audit(
                        path=str(root),
                        axis=charter.split_basis,
                        axis_question=charter.split_question,
                        name=proposed,
                        document=(document_id, descriptions[document_id]),
                    ),
                    choices=("BELONG", "STAY"),
                    max_tokens=8,
                    temperature=0.0,
                )

            verdicts = await _bounded_gather(ordered, verify)
            log_trace(
                "subdivide.rebalance_audit",
                current=current,
                proposed=proposed,
                family=list(ordered),
                comparison=comparison,
                verdicts=verdicts,
            )
            if any(verdict != "BELONG" for verdict in verdicts):
                return None
            return current, proposed, ordered

        decisions = await _bounded_gather(units, review)
        approved = [decision for decision in decisions if decision is not None]
        if not approved:
            return None

        operations: list[Operation] = []
        affected: list[PurePosixPath] = []
        taken_by_target: dict[PurePosixPath, set[str]] = {}
        moved = 0
        for _, proposed, document_ids in approved:
            target = root / proposed
            taken = taken_by_target.setdefault(
                target,
                {
                    path.name.casefold()
                    for path in self._vault.iter_files(target, recursive=False)
                },
            )
            affected.append(target)
            for document_id in document_ids:
                source = contents.path_of(document_id)
                if source is None or len(source.parts) != 2:
                    return None
                filename = _free_filename(source.name, taken)
                taken.add(filename.casefold())
                operations.append(
                    Operation(
                        kind=OperationKind.MOVE,
                        source=source,
                        target=target / filename,
                        note="rebalance current-upload document after sibling growth",
                    )
                )
                source_sidecar = source.parent / sidecar_name(source.name)
                if self._vault.exists(source_sidecar):
                    operations.append(
                        Operation(
                            kind=OperationKind.MOVE,
                            source=source_sidecar,
                            target=target / sidecar_name(filename),
                            note="rebalance sidecar after sibling growth",
                        )
                    )
                moved += 1

        note_operations, payloads = self._stable_child_note_operations(
            root, axis=charter.split_basis
        )
        operations.extend(note_operations)
        self._transactor.execute(
            JournalEntry(
                actor=Actor.BISMUTH,
                reason=f"rebalance {moved} current-upload documents after sibling growth",
                operations=tuple(operations),
            ),
            payloads=payloads,
        )
        targets = tuple(dict.fromkeys(affected))
        log_trace(
            "subdivide.rebalanced",
            moved=moved,
            targets=[str(target) for target in targets],
        )
        return Divided(
            folder=root,
            created=targets,
            moved=moved,
            basis=charter.split_basis,
        )

    async def consider(
        self,
        folder: PurePosixPath,
        *,
        filename: str = "",
        on_progress: ProgressSink | None = None,
        allow_emerging: bool = True,
    ) -> list[Divided]:
        """Draw one class out of ``folder``, if one has grown in it.

        At most one folder is created per call, and never below the one it creates.

        ``filename`` is the document whose arrival prompted this, carried only so the
        progress events join that document's run rather than opening one of their own.
        """
        if folder.parts and folder.parts[0] == INBOX.parts[0]:
            return []  # the inbox holds what could not be read; it is not a category

        contents = self._read(folder)
        self._annotate_family_units(contents)
        charter = self._charter(folder)

        # The measured SLM is reliable at learning broad subject shelves but eagerly
        # creates a single nested child that leaves a confusing mix of one sub-folder
        # and many direct documents.  Keep autonomous growth at the top-level subject
        # catalogue; existing human-authored deeper structure remains usable by placement.
        if folder.parts and not contents.children:
            log_trace(
                "subdivide.skipped",
                folder=str(folder),
                reason="automatic growth is focused on top-level subject shelves",
            )
            return []

        if charter is not None and not charter.managed:
            # A human wrote this note; their structure is not ours to redraw. Traced
            # because "nothing happened here" should never need the source to explain.
            log_trace("subdivide.skipped", folder=str(folder), reason="folder note is not managed")
            return []

        plan = await self._judge(
            folder,
            contents,
            charter,
            filename=filename,
            on_progress=on_progress,
            allow_emerging=allow_emerging,
        )
        if plan is None or not plan.groups:
            return []

        divided = (
            self._replace_boundary(folder, plan, charter)
            if plan.replace_existing
            else (
                self._route_existing(folder, contents, plan, charter)
                if plan.reuse_existing
                else self._apply(folder, contents, plan, charter)
            )
        )
        if not divided.happened:
            return []

        # What was just created is not considered here. It was formed a moment ago from a
        # judgement over these same documents, and asking it again adds no evidence -- it
        # only re-judges. That recursion was worth having when a schedule could leave a
        # new folder unasked for a long time; every arrival asks now, so a child is
        # looked at as soon as anything lands in it. Measured with the recursion still
        # in: a single ingest built 철학/현상학/체화된 인지, one document per level.
        return [divided]

    async def _judge(
        self,
        folder: PurePosixPath,
        contents: _Contents,
        charter: Charter | None,
        *,
        filename: str,
        on_progress: ProgressSink | None,
        allow_emerging: bool,
    ) -> prompts.Division | None:
        """Ask the model. Returns None when there is nothing to ask about."""
        purpose = charter.purpose if charter else ""
        # Through the subtree: dividing moves this folder's documents into its children,
        # so a direct count collapses to nothing and the division is never looked at again.
        total = self._count_documents(folder, recursive=True)

        # Two different jobs, and only the second may move a document that is already
        # filed. Drawing a new class out of the loose pile is additive and safe to ask
        # often; redrawing a boundary is not, and waits for the evidence to double.
        # One child is a provisional shelf, not an established partition. Reviewing it
        # as a complete boundary made the inevitable "not useful navigation" verdict
        # trigger expensive redesigns before a second class had even emerged.
        established_boundary = len(contents.children) >= 2
        if not contents.children and len(contents.documents) < 3:
            # The first recurring class needs two positive examples and at least one
            # loose remainder; otherwise drawing it out merely adds an empty level.
            log_trace(
                "subdivide.skipped",
                folder=str(folder),
                reason="insufficient contrastive evidence",
                documents=len(contents.documents),
            )
            return None
        if (
            charter is not None
            and charter.divided
            and (established_boundary or charter.boundary_review_required)
            and charter.due_for_review(total)
            # Review safety problems postpone only the destructive review. They must
            # never prevent additive filing into the still-usable current structure.
            and len(self._read(folder, recursive=True).documents) == total
            and not self._has_protected_descendant(folder)
        ):
            review_contents = self._read(folder, recursive=True)
            if len(review_contents.documents) != total:
                log_trace(
                    "subdivide.skipped",
                    folder=str(folder),
                    reason="boundary review requires a card for every document",
                    documents=total,
                    cards=len(review_contents.documents),
                )
                return None
            if self._has_protected_descendant(folder):
                log_trace(
                    "subdivide.skipped",
                    folder=str(folder),
                    reason="boundary review contains a protected or unreadable descendant",
                )
                return None
            report(
                on_progress,
                Progress(stage=Stage.REVIEWING, filename=filename, note=str(folder) or "/"),
            )
            current_groups = self._existing_boundary_groups(folder, review_contents)
            direct_signs = [(group.name, group.note) for group in current_groups]
            review = await self._review_boundary(
                folder=folder,
                purpose=purpose,
                charter=charter,
                total=total,
                documents=review_contents.lines,
                children=direct_signs,
            )
            log_trace(
                "subdivide.review",
                folder=str(folder),
                basis=charter.split_basis,
                before=charter.split_at_documents,
                now=total,
                holds=review.holds,
                one_axis=review.one_axis,
                coherent_membership=review.coherent_membership,
                useful_navigation=review.useful_navigation,
            )
            # Schema upgrades force one complete semantic audit of every learned
            # boundary. Previously a failed legacy audit simply returned, leaving the
            # known-bad tree in place forever. A failed audit is evidence that the
            # boundary does not hold, so it must enter the same replacement path as a
            # failed review.
            current_boundary_holds = review.holds
            observed_failures = [
                name
                for name, passed in (
                    ("current signs do not follow one axis", review.one_axis),
                    ("current membership is incoherent", review.coherent_membership),
                    ("current signs do not improve navigation", review.useful_navigation),
                )
                if not passed
            ]
            if charter.boundary_review_required:
                current_audit = await self._audit_boundary(
                    folder=folder,
                    documents=review_contents.lines,
                    axis=charter.split_basis,
                    axis_question=charter.split_question,
                    groups=current_groups,
                    complete=True,
                )
                current_boundary_holds = current_boundary_holds and current_audit.accepted
                observed_failures.extend(_failed_boundary_checks(current_audit))
                log_trace(
                    "subdivide.current_boundary_audit",
                    folder=str(folder),
                    accepted=current_audit.accepted,
                    failed_checks=_failed_boundary_checks(current_audit),
                )

            if not current_boundary_holds:
                replacement_plan = await self._attempt_boundary_replacement(
                    folder=folder,
                    purpose=purpose,
                    charter=charter,
                    total=total,
                    documents=review_contents.lines,
                    current_groups=current_groups,
                    observed_failures=observed_failures,
                    children=direct_signs,
                )
                if replacement_plan is not None:
                    return replacement_plan
                # A failed repair is state, not a reason to starve ordinary filing.
                # Persist the attempt so the same unchanged subtree is not redesigned
                # on every arrival, then continue into existing routing/emergence below.
                self._record_review_attempt(folder, charter, documents=total, repair_pending=True)
            # A holding review is still a judgement made at this size, and it has to be
            # recorded as one. Left unwritten, the folder stays past its doubling for
            # ever: it was asked on every ingest from then on -- fourteen times in a row
            # on one run, all of them holding -- and, worse, the answer returned here so
            # the folder was never asked what else had grown in it.
            else:
                self._rearm(
                    folder,
                    charter,
                    documents=total,
                    axis_question=charter.split_question,
                )

        if not allow_emerging:
            log_trace(
                "subdivide.skipped",
                folder=str(folder),
                reason="ancestor loose pile unchanged",
            )
            return None

        if not contents.documents:
            log_trace("subdivide.skipped", folder=str(folder), reason="nothing sitting here")
            return None

        report(
            on_progress, Progress(stage=Stage.DIVIDING, filename=filename, note=str(folder) or "/")
        )

        if (
            self._BULK_ROUTE_EXPLICITLY_LOOSE_DOCUMENTS
            and charter is not None
            and charter.divided
            and charter.split_basis
            and charter.split_question
            and contents.children
        ):
            assignments = await self._existing_assignments(
                folder=folder, contents=contents, charter=charter
            )
            log_trace(
                "subdivide.existing_assignments",
                folder=str(folder),
                groups=[group.folder_id for group in assignments.groups],
                claimed=sum(len(group.document_ids) for group in assignments.groups),
            )
            if assignments.groups:
                child_handles = {
                    f"F{index:03d}": (name, note)
                    for index, (name, note) in enumerate(contents.children, start=1)
                }
                unknown_handles = sorted(
                    {
                        group.folder_id.strip().upper()
                        for group in assignments.groups
                        if group.folder_id.strip().upper() not in child_handles
                    }
                )
                if unknown_handles:
                    log_trace(
                        "subdivide.rejected",
                        folder=str(folder),
                        reason="unknown existing folder handle",
                        proposed=unknown_handles,
                    )
                    assignments = prompts.ExistingAssignments(groups=[])
                resolved_groups = [
                    prompts.Group(
                        name=child_handles[group.folder_id.strip().upper()][0],
                        note=child_handles[group.folder_id.strip().upper()][1],
                        document_ids=group.document_ids,
                    )
                    for group in assignments.groups
                ]
            else:
                resolved_groups = []
            if resolved_groups:
                coherent_existing = self._cohere_families(contents, resolved_groups)
                if coherent_existing is None:
                    log_trace(
                        "subdivide.rejected",
                        folder=str(folder),
                        reason="document family would be split across existing siblings",
                    )
                    resolved_groups = []
                else:
                    resolved_groups = coherent_existing

            if resolved_groups:

                preview = validate_plan(
                    axis=charter.split_basis,
                    axis_question=charter.split_question,
                    groups=tuple(
                        ProposedClass(name=group.name, document_ids=tuple(group.document_ids))
                        for group in resolved_groups
                    ),
                    available_document_ids=frozenset(
                        document_id for document_id, _, _ in contents.documents
                    ),
                    allow_single_document=True,
                    allow_no_division=True,
                )
                if preview.accepted:
                    routing_audit = await self._audit_routing(
                        folder=folder,
                        documents=contents.lines,
                        charter=charter,
                        groups=resolved_groups,
                        children=contents.children,
                    )
                    if routing_audit.accepted:
                        return prompts.Division(
                            basis=charter.split_basis,
                            basis_question=charter.split_question,
                            groups=resolved_groups,
                            reuse_existing=True,
                        )
                    log_trace(
                        "subdivide.rejected",
                        folder=str(folder),
                        reason="existing assignment audit failed",
                        failed_checks=_failed_routing_checks(routing_audit),
                    )
                else:
                    reasons = [problem.value for problem in preview.problems]
                    log_trace(
                        "subdivide.rejected",
                        folder=str(folder),
                        reason="; ".join(reasons),
                        proposed=[group.name for group in resolved_groups],
                    )

        # Asked on every arrival, and it is the arrival that makes it worth asking. A
        # power-of-two schedule was tried: the root of a thirty-document archive was asked
        # at 2, 4, 8 and 16, declined all four -- correctly, the classes had not gathered
        # yet -- and then waited for a thirty-second document that never came. Fourteen
        # documents arrived unasked and nothing was ever filed. The schedule was built for
        # the old question, "how would you divide this", which has an answer every time
        # and so slipped into yes if asked often enough. This one declines and keeps
        # declining, so there is nothing to ration.
        #
        # Establish the first axis from one recurring class, then draw only that class
        # out.  This is deliberately not a partition: unrelated documents stay loose,
        # and later arrivals may establish another answer to the recorded question.
        # The former multi-sign bootstrap classified the whole loose pile in disjoint
        # packets; on the measured legal corpus that repeatedly forced nearest-fit
        # assignments and made one bad packet invalidate every otherwise useful class.
        # The axis this folder was divided along, if it has been. Every sub-folder here
        # is one answer to it, and a later class has to answer the same question --
        # otherwise the siblings sit on different distinctions and no name rules anything
        # out.
        axis = charter.split_basis if charter is not None else ""
        spent = self._axes_above(folder)

        direct_documents = len(contents.documents)
        # An explicit maintenance/finalization call (no triggering filename) is a
        # deliberate request to inspect the current evidence and bypasses ingest-time
        # throttling. Ordinary per-document calls carry a filename and are gated.
        if filename and not self._initial_boundary_due(folder, direct_documents):
            return None
        # This gate applies after children exist too. A loose root document is still
        # new evidence, but asking the identical "what other class emerged?" question
        # after every single arrival repeated failed candidates dozens of times.
        # Record before the call: provider/schema failures are judgements at this size.
        self._initial_boundary_attempt_at[folder] = direct_documents

        decision_documents = (
            self._initial_boundary_lines(contents) if not contents.children else contents.lines
        )

        emerging = await self._find_emerging(
            folder=folder,
            purpose=purpose,
            documents=decision_documents,
            children=contents.children,
            axis=axis,
            axis_question=charter.split_question if charter is not None else "",
            spent=spent,
            recently_rejected=self._recent_candidates(folder, direct_documents),
        )
        model_axis = emerging.axis
        model_axis_question = emerging.axis_question
        effective_axis = axis
        effective_question = charter.split_question if charter is not None else ""
        if not effective_axis:
            hangul_documents = sum(
                bool(re.search(r"[가-힣]", description))
                for _, description in decision_documents
            )
            korean = bool(decision_documents) and hangul_documents * 2 >= len(decision_documents)
            if folder.parts:
                effective_axis = "세부 주제" if korean else "Detailed subject"
                effective_question = (
                    "이 문서의 주된 세부 주제는 무엇인가?"
                    if korean
                    else "What detailed subject is this document primarily about?"
                )
            else:
                effective_axis = "주제 분야" if korean else "Subject domain"
                effective_question = (
                    "이 문서의 주된 주제 분야는 무엇인가?"
                    if korean
                    else "What subject domain is this document primarily about?"
                )
            emerging = emerging.model_copy(
                update={"axis": effective_axis, "axis_question": effective_question}
            )
        log_trace(
            "subdivide.emerging",
            folder=str(folder),
            documents=len(contents.documents),
            subtree=total,
            axis=effective_axis,
            model_axis=model_axis,
            model_axis_question=model_axis_question,
            axis_is_new=not axis,
            emerged=emerging.emerged,
            name=emerging.name,
        )
        if not emerging.emerged or not emerging.name.strip():
            return None
        self._remember_candidate(folder, emerging.name, documents=direct_documents)

        proposed = effective_axis.strip()
        if not axis and proposed and _same_axis(proposed, spent):
            # Everything in this folder shares the ancestors' answer to their axes -- that
            # is what put these documents together -- so those axes cannot tell any of
            # them apart. Reusing one creates a repeated, non-distinguishing boundary.
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="axis already used above here",
                proposed=[proposed],
                spent=spent,
            )
            return None

        members = await self._find_members(
            folder=folder,
            purpose=purpose,
            documents=decision_documents,
            children=contents.children,
            name=emerging.name,
            note=boundary_purpose(effective_axis, emerging.name),
        )
        log_trace(
            "subdivide.members",
            folder=str(folder),
            name=emerging.name,
            claimed=len(members.document_ids),
            of=len(contents.documents),
        )
        if not members.document_ids:
            return None

        proposed_groups = [
            prompts.Group(
                name=emerging.name,
                note=boundary_purpose(effective_axis, emerging.name),
                document_ids=members.document_ids,
            )
        ]
        coherent = self._cohere_families(contents, proposed_groups)
        if coherent is None:
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="document family would be split across proposed siblings",
            )
            return None
        proposed_groups = coherent

        # Root signs are normalised only after membership exists.  Passing the whole
        # loose pile here made unrelated contrast documents dominate Qwen's answer
        # (for example, a consumer candidate was renamed to science).  The sign is a
        # claim about this proposed class, so only the claimed class may ground it.
        if not folder.parts:
            original_root_candidate = emerging.name
            selected_ids = set(proposed_groups[0].document_ids)
            selected_evidence = [
                (document_id, description)
                for document_id, description in decision_documents
                if document_id in selected_ids
            ]
            normalized = await self._llm.structured(
                prompts.build_normalized_root_sign(
                    candidate=original_root_candidate,
                    documents=selected_evidence,
                ),
                schema=prompts.NormalizedSign,
            )
            log_trace(
                "subdivide.sign_normalized",
                folder=str(folder),
                original=original_root_candidate,
                normalized=normalized.name,
                valid=normalized.valid,
                evidence_ids=sorted(selected_ids),
            )
            if not normalized.valid or not normalized.name.strip():
                return None
            normalized_name = normalized.name.strip()
            if not _root_normalization_is_grounded(
                original_root_candidate, normalized_name
            ):
                log_trace(
                    "subdivide.sign_normalization_ungrounded",
                    folder=str(folder),
                    original=original_root_candidate,
                    rejected=normalized_name,
                    evidence_ids=sorted(selected_ids),
                )
                return None
            emerging = emerging.model_copy(update={"name": normalized_name})
            proposed_groups[0] = proposed_groups[0].model_copy(
                update={
                    "name": normalized_name,
                    "note": boundary_purpose(effective_axis, normalized_name),
                }
            )

        if not folder.parts and normalise_label(emerging.name) in _GENERIC_ROOT_SIGNS:
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="root sign is too generic to narrow navigation",
                proposed=[emerging.name],
            )
            return None

        # A class name is one answer, never a comparison or a slash-joined list of
        # possible answers.  This is a language-neutral syntactic invariant; it does
        # not encode any corpus category names.
        if (
            "/" in emerging.name
            or "\\" in emerging.name
            or "&" in emerging.name
            or (not folder.parts and re.search(r"(?:^|\s)및(?:\s|$)", emerging.name))
            or re.search(r"(?i)(?:^|\s)(?:vs\.?|versus)(?:\s|$)", emerging.name)
        ):
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="class sign compares or joins multiple answers",
                proposed=[emerging.name],
            )
            return None

        if normalise_label(emerging.name) in {
            normalise_label(name) for name, _ in contents.children
        }:
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="proposed class already exists as a direct child",
                proposed=[emerging.name],
            )
            return None

        if problem := self._explicit_card_value_problem(contents, proposed_groups):
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason=problem,
                proposed=[group.name for group in proposed_groups],
            )
            return None
        preview = validate_plan(
            axis=effective_axis,
            axis_question=effective_question,
            groups=tuple(
                ProposedClass(name=group.name, document_ids=tuple(group.document_ids))
                for group in proposed_groups
            ),
            available_document_ids=frozenset(
                document_id for document_id, _, _ in contents.documents
            ),
            ancestor_names=folder.parts,
            spent_axes=tuple(spent),
        )
        if not preview.accepted:
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="; ".join(problem.value for problem in preview.problems),
                proposed=[group.name for group in proposed_groups],
            )
            return None
        # Additive growth owns only the loose documents it is about to move.  Re-auditing
        # every old sibling here coupled two independent concerns: one stale membership
        # in an existing shelf permanently blocked a clean new class.  Whole-boundary
        # coherence remains the responsibility of the scheduled review/replacement path.
        class_audit = await self._audit_class(
            folder=folder,
            documents=decision_documents,
            axis=effective_axis,
            axis_question=effective_question,
            group=proposed_groups[0],
            children=contents.children,
        )
        unary_checks = {
            "name_answers_question": class_audit.name_answers_question,
            "recurring_class": class_audit.recurring_class,
            "useful_for_navigation": class_audit.useful_for_navigation,
            "distinct_from_contrast": class_audit.distinct_from_contrast,
        }
        if not all(unary_checks.values()):
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="new class semantic audit failed",
                failed_checks=[name for name, passed in unary_checks.items() if not passed],
            )
            return None
        if class_audit.invalid_member_ids:
            invalid = set(class_audit.invalid_member_ids)
            for family in self._document_families(contents):
                if family & invalid:
                    invalid.update(family)
            proposed_groups[0].document_ids = [
                document_id
                for document_id in proposed_groups[0].document_ids
                if document_id not in invalid
            ]
            log_trace(
                "subdivide.members_refined",
                folder=str(folder),
                name=emerging.name,
                removed=sorted(invalid),
                remaining=len(proposed_groups[0].document_ids),
            )
            preview = validate_plan(
                axis=effective_axis,
                axis_question=effective_question,
                groups=tuple(
                    ProposedClass(name=group.name, document_ids=tuple(group.document_ids))
                    for group in proposed_groups
                ),
                available_document_ids=frozenset(
                    document_id for document_id, _, _ in contents.documents
                ),
                ancestor_names=folder.parts,
                spent_axes=tuple(spent),
            )
            if not preview.accepted:
                log_trace(
                    "subdivide.rejected",
                    folder=str(folder),
                    reason="; ".join(problem.value for problem in preview.problems),
                    proposed=[group.name for group in proposed_groups],
                )
                return None
        independent_units = self._independent_family_units(
            contents, set(proposed_groups[0].document_ids)
        )
        if independent_units < 2:
            log_trace(
                "subdivide.rejected",
                folder=str(folder),
                reason="new class contains fewer than two independent document families",
                proposed=[emerging.name],
                documents=len(proposed_groups[0].document_ids),
                independent_families=independent_units,
            )
            return None
        log_trace(
            "subdivide.class_validated",
            folder=str(folder),
            axis=axis or emerging.axis.strip(),
            name=emerging.name,
            members=len(proposed_groups[0].document_ids),
            first=not contents.children,
        )

        self._initial_boundary_attempt_at.pop(folder, None)
        return prompts.Division(
            # The axis, not a sentence about this one extraction. It is read back on the
            # next look and on review, and it is what holds the siblings to one question.
            basis=effective_axis or emerging.name,
            basis_question=effective_question,
            groups=proposed_groups,
        )
