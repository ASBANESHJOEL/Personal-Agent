"""One-shot scheduled discovery batch entry point for a Render Cron Job."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import sys
from typing import Any

from . import discovery
from .config import Settings, get_settings
from .scheduled_store import ScheduledStoreError, ScheduledWorkerStore


@dataclass(frozen=True)
class BatchResult:
    enabled_users: int
    claimed: int
    succeeded: int
    partially_failed: int
    skipped: int
    failed: int
    coalesced: int


def run_scheduled_batch(
    settings: Settings | None = None,
    store: Any | None = None,
    now: datetime | None = None,
) -> BatchResult:
    settings = settings or get_settings()
    if settings.agent_state_storage != "supabase":
        raise ScheduledStoreError("Scheduled discovery requires AGENT_STATE_STORAGE=supabase.")
    store = store or ScheduledWorkerStore(settings)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    idempotency_key = f"scheduled-discovery:{now.date().isoformat()}"
    user_ids = store.enabled_user_ids()
    claimed = succeeded = partially_failed = skipped = failed = coalesced = 0

    for user_id in user_ids:
        run_id: str | None = None
        claimed_row = None
        try:
            claimed_row = store.claim_run(user_id, idempotency_key)
            if claimed_row is None:
                coalesced += 1
                continue
            run_id = claimed_row.get("id")
            if not isinstance(run_id, str) or not run_id:
                raise ScheduledStoreError("Supabase did not return a valid scheduled run claim.")
            claimed += 1

            try:
                paused = store.get_pause_state(user_id)
            except Exception:
                store.finish_run(
                    run_id, user_id, status="skipped", source_results=[],
                    new_discoveries=0, updated_discoveries=0,
                    error_summary="Global Pause state could not be verified; user skipped.",
                )
                skipped += 1
                continue
            if paused:
                store.finish_run(
                    run_id, user_id, status="skipped", source_results=[],
                    new_discoveries=0, updated_discoveries=0,
                    error_summary="Global Pause is on; user skipped.",
                )
                skipped += 1
                continue

            request = discovery.DiscoveryRunRequest(
                query="",
                platforms=["github", "huggingface"],
                hf_types=["model", "dataset", "space"],
                limit=20,
                page=1,
            )
            items, sources, _ = discovery.collect_discovery(request, settings)
            source_results = [source.model_dump(mode="json") for source in sources]
            successful_sources = sum(source.status == "ok" for source in sources)
            failed_sources = len(sources) - successful_sources
            new_count = store.persist_items(user_id, items)
            if failed_sources and successful_sources:
                status = "partially_failed"
                error = "One or more discovery providers failed; successful provider results were retained."
            elif failed_sources:
                status = "failed"
                error = "All discovery providers failed; no provider results were retained."
            else:
                status = "succeeded"
                error = None
            store.finish_run(
                run_id, user_id, status=status, source_results=source_results,
                new_discoveries=new_count, updated_discoveries=0, error_summary=error,
            )
            if status == "succeeded":
                succeeded += 1
            elif status == "partially_failed":
                partially_failed += 1
            else:
                failed += 1
        except Exception:
            # Never log provider bodies, access tokens, service keys, or arbitrary
            # exception text. A bad user's run cannot stop other opted-in users.
            if run_id:
                try:
                    store.finish_run(
                        run_id, user_id, status="failed", source_results=[],
                        new_discoveries=0, updated_discoveries=0,
                        error_summary="Scheduled discovery failed while accessing cloud storage or processing provider data.",
                    )
                except Exception:
                    pass
            failed += 1

    return BatchResult(len(user_ids), claimed, succeeded, partially_failed, skipped, failed, coalesced)


def main() -> int:
    try:
        result = run_scheduled_batch()
    except ScheduledStoreError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("Scheduled discovery could not enumerate enabled users safely.", file=sys.stderr)
        return 1
    print(
        "Scheduled discovery batch complete: "
        f"users={result.enabled_users} claimed={result.claimed} succeeded={result.succeeded} "
        f"partial={result.partially_failed} skipped={result.skipped} failed={result.failed} "
        f"coalesced={result.coalesced}"
    )
    return 0 if result.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
