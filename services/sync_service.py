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

has_more: a single flag meaning "the server had more changes beyond this
batch" (events OR memory), NOT per-resource pagination — that's left for
later. Computed from a true remaining-count check (count_since > what was
returned), not from "did we hit the limit", so it's never a false
positive when exactly `limit` items happened to be the total remainder.
"""
from services import event_service, memory_service


def apply_sync(device_id: str, cursor: int, events: list[dict], memory: list[dict],
               limit: int) -> dict:
    accepted_events = []
    for item in events:
        row, created = event_service.record_for_sync(
            device_id=item["device_id"],
            event_type=item["event_type"],
            payload=item["payload"],
            occurred_at=item["occurred_at"],
            event_uid=item["event_uid"],
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

    delivered_seqs = [r["seq"] for r in new_events] + [r["seq"] for r in new_memory]
    next_cursor = max([cursor, *delivered_seqs])

    total_events = event_service.count_since(cursor)
    total_memory = memory_service.count_since(cursor)
    has_more = total_events > len(new_events) or total_memory > len(new_memory)

    return {
        "device_id": device_id,
        "status": "ok",
        "accepted_events": accepted_events,
        "memory_results": memory_results,
        "changes": {"events": new_events, "memory": new_memory},
        "cursor": next_cursor,
        "has_more": has_more,
    }
