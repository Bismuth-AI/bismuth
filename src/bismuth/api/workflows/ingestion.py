"""Application workflow for ingesting documents and maintaining bounded structure windows."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import PurePosixPath

from bismuth.api.maintenance import MaintenanceState
from bismuth.api.maintenance import load as load_maintenance
from bismuth.api.maintenance import save as save_maintenance
from bismuth.api.models import IngestOut, result_of
from bismuth.api.progress import ProgressBus
from bismuth.container import Bismuth
from bismuth.domain.errors import BismuthError
from bismuth.domain.progress import Progress, Stage
from bismuth.logging_setup import log_context, log_trace
from bismuth.ports.llm import Spend
from bismuth.services.maintenance_windows import family_closure, next_window

logger = logging.getLogger(__name__)


def _work_candidates(
    engine: Bismuth,
    state: MaintenanceState,
    *,
    exclude: set[str] | None = None,
) -> list[str]:
    """Return new arrivals plus only the exact family mates needed to act safely.

    An earlier placed or deferred document may re-enter only as the grounded mate of an
    unprocessed arrival.  The 30-document packer then moves unrelated arrivals to the next
    window.  This is family closure, not automatic replay of an old failed window.
    """

    excluded = exclude or set()
    pending = [
        document_id for document_id in state.pending_document_ids if document_id not in excluded
    ]
    return family_closure(engine.catalog, pending)


def _next_deferred(
    previous: list[str], selected: list[str], unresolved: tuple[str, ...]
) -> list[str]:
    """Remove successfully filed retry candidates and retain only unresolved ones."""

    selected_ids = set(selected)
    return list(
        dict.fromkeys(
            [
                *(document_id for document_id in previous if document_id not in selected_ids),
                *unresolved,
            ]
        )
    )


class IngestWorkflow:
    """Coordinates ingest, durable arrival queues, and isolated structure windows."""

    def __init__(self, progress: ProgressBus) -> None:
        self._progress = progress

    async def process(
        self,
        engine: Bismuth,
        rel: PurePosixPath,
        *,
        on_progress: Callable[[Progress], None] | None = None,
    ) -> IngestOut:
        publish = on_progress or self._progress.publish
        self.drain_spend(engine)  # anything left from an earlier document is not this one's bill
        try:
            result = await engine.ingest.process(rel, on_progress=publish)
        except BismuthError as exc:
            publish(Progress(stage=Stage.FAILED, filename=rel.name, note=str(exc)))
            return IngestOut(
                filename=rel.name, ok=False, reason=str(exc), spend=self.drain_spend(engine)
            )
        return result_of(result, spend=self.drain_spend(engine))

    @staticmethod
    def is_new_arrival(result: IngestOut) -> bool:
        return result.ok and result.placed and not result.duplicate and bool(result.document_id)

    def enqueue_documents(
        self, engine: Bismuth, document_ids: list[str], *, source: str
    ) -> MaintenanceState:
        previous = load_maintenance(engine.vault.root)
        # A failed automatic window never blocks a later upload and is never retried
        # implicitly. Archive its IDs as deferred, then start the new upload's queue.
        if previous.status == "failed":
            deferred = list(
                dict.fromkeys([*previous.deferred_document_ids, *previous.pending_document_ids])
            )
            backlog: list[str] = []
        else:
            backlog = list(dict.fromkeys(previous.pending_document_ids))
            deferred = list(dict.fromkeys(previous.deferred_document_ids))
        seen = set(backlog)
        for document_id in document_ids:
            if document_id in seen:
                continue
            backlog.append(document_id)
            seen.add(document_id)
        new_cycle = (
            not previous.pending_document_ids
            and not previous.deferred_document_ids
            and previous.status != "failed"
            and previous.source != source
        )
        waiting = previous.model_copy(
            update={
                "status": "waiting",
                "source": source,
                "error": "",
                "summary": "" if new_cycle else previous.summary,
                "moved": 0 if new_cycle else previous.moved,
                "applied": False if new_cycle else previous.applied,
                "pending_document_ids": backlog,
                "deferred_document_ids": deferred,
                "completed_windows": 0 if new_cycle else previous.completed_windows,
                "review_round": 1 if new_cycle else previous.review_round,
                "current_window_documents": 0,
                "finished_at": None,
            }
        )
        save_maintenance(engine.vault.root, waiting)
        return waiting

    @staticmethod
    def tail_due(engine: Bismuth, arrivals: int, state: MaintenanceState) -> bool:
        del engine
        # Full windows are drained during ingestion. Once this upload/scan ends, flush
        # its final 1-29 arrivals as one bounded tail (153 files => 30*5 + 3).
        return arrivals > 0 and bool(state.pending_document_ids)

    async def drain_maintenance(
        self,
        engine: Bismuth,
        *,
        source: str,
        max_windows: int | None = None,
    ) -> MaintenanceState:
        """Process queued arrivals in isolated windows, updating the tree between them."""
        processed = 0
        attempted_this_drain: set[str] = set()
        initial = load_maintenance(engine.vault.root)
        reviewed_scope_fingerprints = {
            scope: fingerprint
            for scope, fingerprint in initial.reviewed_scope_fingerprints.items()
            if engine.agent.scope_fingerprint(scope)
        }
        while True:
            previous = load_maintenance(engine.vault.root)
            if not previous.pending_document_ids:
                return previous
            window = next_window(
                engine.catalog,
                _work_candidates(engine, previous, exclude=attempted_this_drain),
            )
            if not window:
                return previous
            attempted_this_drain.update(window)
            pending_ids = set(previous.pending_document_ids)
            new_window_ids = [item for item in window if item in pending_ids]
            retried_window_ids = [item for item in window if item not in pending_ids]
            running = previous.model_copy(
                update={
                    "status": "running",
                    "source": source,
                    "error": "",
                    "attempts": previous.attempts + 1,
                    "current_window_documents": len(window),
                    "started_at": time.time(),
                    "finished_at": None,
                }
            )
            save_maintenance(engine.vault.root, running)
            logger.info(
                "maintenance window started: source=%s round=%d window=%d documents=%d queued=%d",
                source,
                running.review_round,
                running.completed_windows + 1,
                len(window),
                len(running.pending_document_ids),
            )
            window_id = f"{source}:window-{running.completed_windows + 1:03d}"
            log_trace(
                "maintenance.window_started",
                workflow_id=source,
                window_id=window_id,
                window_number=running.completed_windows + 1,
                document_ids=window,
                new_document_ids=new_window_ids,
                retried_deferred_document_ids=retried_window_ids,
                queued_documents=len(running.pending_document_ids),
                deferred_documents=len(running.deferred_document_ids),
            )
            try:
                with log_context(
                    workflow_id=source,
                    window_id=window_id,
                    window_number=running.completed_windows + 1,
                ):
                    result = await engine.agent.reorganize(focus_document_ids=window)
            except asyncio.CancelledError:
                failed = running.model_copy(
                    update={
                        "status": "failed",
                        "error": (
                            "The server stopped while organizing. "
                            "The affected arrivals were preserved as deferred documents."
                        ),
                        "current_window_documents": 0,
                        "finished_at": time.time(),
                    }
                )
                save_maintenance(engine.vault.root, failed)
                log_trace(
                    "maintenance.window_failed",
                    workflow_id=source,
                    window_id=window_id,
                    window_number=running.completed_windows + 1,
                    error=failed.error,
                    cancelled=True,
                )
                raise
            except Exception as exc:
                logger.exception("autonomous library maintenance failed; preserving current tree")
                failed = running.model_copy(
                    update={
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                        "current_window_documents": 0,
                        "finished_at": time.time(),
                    }
                )
                save_maintenance(engine.vault.root, failed)
                log_trace(
                    "maintenance.window_failed",
                    workflow_id=source,
                    window_id=window_id,
                    window_number=running.completed_windows + 1,
                    error=failed.error,
                    cancelled=False,
                )
                return failed
            finally:
                self.drain_spend(engine)

            problems = list(result.proposal.problems)
            summary = result.proposal.summary
            moved = result.moved
            applied = result.applied
            unresolved_document_ids = result.unresolved_document_ids

            # The root planner maintains the common boundary. Give one affected leaf
            # its own clean context as well, so a large upload grows depth rather than
            # asking one root conversation to understand every subtree at once.
            unchanged_reviewed_scopes = {
                scope
                for scope, fingerprint in reviewed_scope_fingerprints.items()
                if fingerprint and engine.agent.scope_fingerprint(scope) == fingerprint
            }
            if not problems and (
                leaf := engine.agent.next_affected_scope(window, exclude=unchanged_reviewed_scopes)
            ):
                leaf_scope, leaf_ids = leaf
                logger.info(
                    "maintenance leaf pass started: scope=%s documents=%d",
                    leaf_scope,
                    len(leaf_ids),
                )
                try:
                    with log_context(
                        workflow_id=source,
                        window_id=window_id,
                        window_number=running.completed_windows + 1,
                    ):
                        leaf_result = await engine.agent.reorganize(
                            scope=leaf_scope,
                            focus_document_ids=leaf_ids,
                        )
                except Exception as exc:
                    logger.exception(
                        "maintenance leaf pass failed; preserving the applied parent pass"
                    )
                    summary = "\n\n".join(
                        part
                        for part in (
                            summary.strip(),
                            (
                                f"Deeper review of {leaf_scope} was deferred: "
                                f"{type(exc).__name__}: {exc}"
                            ),
                        )
                        if part
                    )
                    leaf_result = None
                finally:
                    self.drain_spend(engine)
                if leaf_result is not None:
                    moved += leaf_result.moved
                    applied = applied or leaf_result.applied
                    if leaf_result.proposal.summary.strip():
                        summary = "\n\n".join(
                            part
                            for part in (
                                summary.strip(),
                                leaf_result.proposal.summary.strip(),
                            )
                            if part
                        )
                    if leaf_result.proposal.problems:
                        problems.extend(
                            f"{leaf_scope}: {problem}" for problem in leaf_result.proposal.problems
                        )
                    else:
                        reviewed_scope_fingerprints[leaf_scope] = engine.agent.scope_fingerprint(
                            leaf_scope
                        )
                    logger.info(
                        "maintenance leaf pass finished: scope=%s moved=%d problems=%d",
                        leaf_scope,
                        leaf_result.moved,
                        len(leaf_result.proposal.problems),
                    )

            if not applied and (problems or not summary.strip()):
                detail = "; ".join(problems) or "planner returned no plan and no explanation"
                failed = running.model_copy(
                    update={
                        "status": "failed",
                        "error": f"Structure plan was not completed: {detail}",
                        "summary": summary,
                        "current_window_documents": 0,
                        "finished_at": time.time(),
                    }
                )
                save_maintenance(engine.vault.root, failed)
                return failed

            consumed = set(window)
            remaining = [
                document_id
                for document_id in running.pending_document_ids
                if document_id not in consumed
            ]
            unresolved = _next_deferred(
                running.deferred_document_ids,
                window,
                unresolved_document_ids,
            )
            processed += 1
            complete = not remaining and not unresolved
            partial = not remaining and bool(unresolved)
            state = running.model_copy(
                update={
                    "status": "done" if complete else "partial" if partial else "waiting",
                    "summary": summary,
                    "moved": running.moved + moved,
                    "applied": running.applied or applied,
                    "pending_document_ids": remaining,
                    "deferred_document_ids": unresolved,
                    "completed_windows": running.completed_windows + 1,
                    "review_round": running.review_round,
                    "reviewed_scope_fingerprints": reviewed_scope_fingerprints,
                    "current_window_documents": 0,
                    "finished_at": time.time() if complete or partial else None,
                }
            )
            save_maintenance(engine.vault.root, state)
            logger.info(
                "maintenance window finished: source=%s round=%d window=%d moved=%d remaining=%d",
                source,
                running.review_round,
                state.completed_windows,
                moved,
                len(remaining) + len(unresolved),
            )
            log_trace(
                "maintenance.window_finished",
                workflow_id=source,
                window_id=window_id,
                window_number=state.completed_windows,
                moved=moved,
                remaining_documents=len(remaining),
                deferred_documents=len(unresolved),
                status=state.status,
                summary=summary,
            )
            if complete or partial or (max_windows is not None and processed >= max_windows):
                return state

    @staticmethod
    def drain_spend(engine: Bismuth) -> Spend:
        """Collect and reset what the models have spent. Documents are processed one at a
        time, so draining around one is what attributes the bill to it.

        Draining is also what keeps the adapters' usage lists from growing for the life of
        the process; before anything read them, nothing ever emptied them.
        """
        spend = Spend.of(engine.llm.drain_usage())
        chat_drain = getattr(engine.chat, "drain_usage", None)
        if chat_drain is not None:  # agentkit's ChatModel protocol does not require it
            spend = spend + Spend.of(chat_drain())
        engine.ledger.record(spend)
        return spend
