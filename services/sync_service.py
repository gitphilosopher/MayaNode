"""services/sync_service.py — orchestrates one /sync exchange.

Stateless between calls: everything the server needs is in this one
request (device_id, cursor, uploaded events/memory) plus the database
itself — no sync "session" is remembered between requests, so a client
can retry a failed call or resume after being offline by simply calling
again with the same or an older cursor.

Each uploaded item commits in its OWN small transaction (not one giant
transaction for the whole batch) — deliberate: if a request dies halfway
through, the client resends the same batch and every already-accepted
item is idempotently absorbed (duplicate/noop) rather than re-applied.

Order per call: apply uploaded events, apply uploaded memory writes, THEN
compute what's newer than the client's cursor — so a client's own
just-accepted uploads are included in `changes` too. Small, harmless
redundancy (the client already knows what it just sent) rather than
extra bookkeeping to exclude them.

Step 2 (see node/protocol.py, docs/PROTOCOL_CONTRACT.md): every uploaded
event is validated against the canonical envelope contract before it's
persisted. A malformed event, an unregistered event_type, or an
unsupported schema_version is reported back as a rejected item — status
"rejected" plus an error_code/error — and simply skipped; every OTHER
item in the same batch still applies exactly as before. The response
also echoes this server's protocol_version and its outgoing 'changes'
arrays are sanity-checked for strictly-increasing seq before being
returned (a defensive check against a server-side ordering bug — never
triggered by client input).

has_more: a single flag meaning "the server had more changes beyond this
batch" (events OR memory), NOT per-resource pagination — that's left for
later. Computed from a true remaining-count check (count_since > what was
returned), not from "did we hit the limit", so it's never a false
positive when exactly `limit` items happened to be the total remainder.
"""
import logging

from node.protocol import stamp_protocol_version, validate_event_batch, validate_change_ordering
from services import event_service, memory_service

logger = logging.getLogger(__name__)


def apply_sync(device_id: str, cursor: int, events: list[dict], memory: list[dict],
               limit: int) -> dict:
    accepted_events = []
    validated = validate_event_batch(events, default_device_id=device_id)
    for raw, result in zip(events, validated):
        if not result.ok:
            accepted_events.append({
                "event_uid": raw.get("event_uid") if isinstance(raw, dict) else None,
                "status": "rejected",
                "error_code": result.error_code,
                "error": result.error,
            })
            continue

        env = result.envelope
        row, created = event_service.record_for_sync(
            device_id=env.device_id or device_id,
            event_type=env.event_type,
            payload=env.payload,
            occurred_at=env.occurred_at,
            event_uid=env.event_uid,
        )
        accepted_events.append({
            "event_uid": row["event_uid"],
            "id": row["id"],
            "seq": row["seq"],
            "status": "accepted" if created else "duplicate",
        })

    memory_results = []
    for item in memory:
        record, status = memory_service.sync_set_value(
            device_id=item["device_id"],
            key=item["key"],
            value=item["value"],
            updated_at=item["updated_at"],
            updated_by=device_id,
        )
        result = {
            "device_id": record["device_id"],
            "key": record["key"],
            "status": status,
            "seq": record["seq"],
            "revision": record["revision"],
        }
        if status == "conflict":
            result["server_record"] = record
        memory_results.append(result)

    new_events = event_service.list_since(cursor, limit=limit)
    new_memory = memory_service.list_since(cursor, limit=limit)

    # Defensive only — this data comes straight from our own DB queries,
    # which already order by seq ASC; a violation here means a genuine
    # server-side bug, not client input. Never let this crash a request.
    try:
        validate_change_ordering(new_events)
        validate_change_ordering(new_memory)
    except ValueError as e:
        logger.error(f"Outgoing /sync changes failed the ordering invariant (server bug): {e}")

    delivered_seqs = [r["seq"] for r in new_events] + [r["seq"] for r in new_memory]
    next_cursor = max([cursor, *delivered_seqs])

    total_events = event_service.count_since(cursor)
    total_memory = memory_service.count_since(cursor)
    has_more = total_events > len(new_events) or total_memory > len(new_memory)

    return stamp_protocol_version({
        "device_id": device_id,
        "status": "ok",
        "accepted_events": accepted_events,
        "memory_results": memory_results,
        "changes": {"events": new_events, "memory": new_memory},
        "cursor": next_cursor,
        "has_more": has_more,
    })
