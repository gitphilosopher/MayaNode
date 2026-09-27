"""
node/protocol.py
=================
MayaNode's own, self-contained implementation of the versioned MayaVE
<-> MayaNode wire contract. See docs/PROTOCOL_CONTRACT.md for the
authoritative JSON/HTTP shape this module implements.

Repository boundary: MayaVE and MayaNode are SEPARATE repositories.
This module must never be imported by MayaVE, and must never import
anything from MayaVE's side (there is no such thing reachable from here
in a real deployment). MayaVE has its own independent implementation of
the same contract at services/node/protocol.py (a different repo).
Compatibility between the two is enforced by both sides conforming to
docs/PROTOCOL_CONTRACT.md and by contract tests that exercise both
implementations against the same fixture payloads — not by shared code.

This module owns:
  - protocol version negotiation (PROTOCOL_VERSION, is_version_supported)
  - the event envelope shape + event-type registry + validation
  - a defensive ordering check for outgoing 'changes' arrays

It has no idea what "mood changed" or "note created" means — only
`protocol.ping` (no application data) is pre-registered. A future
MayaVE-originated event type is added here (server side) and
independently at services/node/protocol.py (client side) in the same
change, per docs/PROTOCOL_CONTRACT.md.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from node import clock

logger = logging.getLogger(__name__)

# ── Versioning ──────────────────────────────────────────────────────────────

PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOL_VERSIONS = frozenset({1})


def is_version_supported(version) -> bool:
    """True if `version` (an int, or anything int()-able) is one this
    server build can speak. Never raises — an unparsable version is
    simply unsupported, not a crash."""
    try:
        return int(version) in SUPPORTED_PROTOCOL_VERSIONS
    except (TypeError, ValueError):
        return False


def stamp_protocol_version(response: dict) -> dict:
    """Sets response['protocol_version'] to this server's current
    version and returns the same dict, for a one-line call site at the
    end of apply_sync()."""
    response["protocol_version"] = PROTOCOL_VERSION
    return response


# ── Errors ────────────────────────────────────────────────────────────────

class ProtocolError(ValueError):
    """Base class for every protocol-layer validation failure."""


class SchemaValidationError(ProtocolError):
    """An envelope or sync payload doesn't match the required shape."""


class UnknownEventTypeError(ProtocolError):
    """event_type isn't in this server's registry."""


class UnsupportedVersionError(ProtocolError):
    """A declared protocol_version or schema_version isn't one this
    server build can speak."""


_ERROR_CODES: dict[type, str] = {
    UnknownEventTypeError: "unknown_event_type",
    UnsupportedVersionError: "unsupported_schema_version",
    SchemaValidationError: "malformed",
}

# ── Envelope shape ────────────────────────────────────────────────────────

_EVENT_UID_MAX_LEN  = 128
_EVENT_TYPE_MAX_LEN = 64
_DEVICE_ID_MAX_LEN  = 64
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class EventTypeSpec:
    """A registered event type's contract: its current payload
    schema_version and the payload fields every event of this type must
    carry (field_name -> expected Python type, or a tuple of types)."""
    name: str
    schema_version: int = 1
    required_fields: dict = field(default_factory=dict)
    description: str = ""


@dataclass(frozen=True)
class EventEnvelope:
    """A validated event, ready to hand to event_service.record_for_sync().
    Immutable — validation either produces one of these or a rejection."""
    event_uid: str
    event_type: str
    occurred_at: str
    payload: dict
    schema_version: int
    device_id: str | None = None

    def dedup_key(self) -> tuple[str, str]:
        return (self.device_id or "", self.event_uid)

    def to_dict(self) -> dict:
        return {
            "event_uid": self.event_uid,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "payload": self.payload,
            "schema_version": self.schema_version,
            "device_id": self.device_id,
        }


@dataclass(frozen=True)
class EventValidationResult:
    """Outcome of validating one raw event dict. `error_code` is a short,
    stable string a caller can branch on (see docs/PROTOCOL_CONTRACT.md's
    'rejected' shape): "malformed", "unknown_event_type",
    "unsupported_schema_version", "internal_error"."""
    ok: bool
    envelope: EventEnvelope | None = None
    event_uid: str | None = None
    error_code: str | None = None
    error: str | None = None


# name -> EventTypeSpec. Real MayaVE event types get registered here
# (server side) the same change they're registered in MayaVE's own
# services/node/protocol.py (client side) — see module docstring.
_REGISTRY: dict[str, EventTypeSpec] = {}


def register_event_type(name: str, *, schema_version: int = 1,
                         required_fields: dict | None = None,
                         description: str = "") -> EventTypeSpec:
    """Declare (or re-declare) an event type. Re-registering the SAME
    name with an IDENTICAL spec is a no-op; a DIFFERENT spec raises
    ProtocolError — a real conflict, not something to silently overwrite."""
    spec = EventTypeSpec(
        name=name, schema_version=schema_version,
        required_fields=dict(required_fields or {}), description=description,
    )
    existing = _REGISTRY.get(name)
    if existing is not None and existing != spec:
        raise ProtocolError(
            f"event type '{name}' is already registered with a different spec "
            f"({existing!r} != {spec!r})"
        )
    _REGISTRY[name] = spec
    return spec


def registered_event_types() -> dict[str, EventTypeSpec]:
    return dict(_REGISTRY)


# Reserved, business-agnostic type — see docs/PROTOCOL_CONTRACT.md.
register_event_type(
    "protocol.ping", schema_version=1, required_fields={},
    description="No-op connectivity/idempotency check event; carries no application data.",
)

# Mirrors services/node/protocol.py's registration (Step 3) — MayaNode
# and MayaVE independently declare the same event type per
# docs/PROTOCOL_CONTRACT.md; there is no shared code between them.
register_event_type(
    "mayave.turn_completed", schema_version=1, required_fields={"intent": str},
    description="A MayaVE conversation turn was resolved to the given intent.",
)


def _check_json_serializable(payload: Any) -> None:
    try:
        json.dumps(payload)
    except (TypeError, ValueError) as e:
        raise SchemaValidationError(f"payload is not JSON-serialisable: {e}") from e


def validate_envelope(data: dict, *, default_device_id: str | None = None) -> EventEnvelope:
    """Validate one raw event dict against the envelope shape and its
    event_type's registered spec. Raises a ProtocolError subclass on any
    failure — use validate_envelope_safe()/validate_event_batch() for a
    non-raising version."""
    if not isinstance(data, dict):
        raise SchemaValidationError(f"event must be an object, got {type(data).__name__}")

    event_uid = data.get("event_uid")
    if not isinstance(event_uid, str) or not event_uid.strip():
        raise SchemaValidationError("event_uid is required and must be a non-empty string")
    if len(event_uid) > _EVENT_UID_MAX_LEN:
        raise SchemaValidationError(f"event_uid exceeds {_EVENT_UID_MAX_LEN} characters")

    event_type = data.get("event_type")
    if not isinstance(event_type, str) or not _EVENT_TYPE_RE.match(event_type):
        raise SchemaValidationError(
            "event_type is required and must look like '<namespace>.<name>' "
            "(lowercase letters/digits/underscore)"
        )
    if len(event_type) > _EVENT_TYPE_MAX_LEN:
        raise SchemaValidationError(f"event_type exceeds {_EVENT_TYPE_MAX_LEN} characters")

    occurred_at = data.get("occurred_at")
    if not isinstance(occurred_at, str) or not occurred_at:
        raise SchemaValidationError("occurred_at is required and must be a string")
    try:
        dt = clock.parse_iso(occurred_at)
    except ValueError as e:
        raise SchemaValidationError(f"occurred_at is not a valid ISO-8601 datetime: {e}") from e
    if dt.tzinfo is None:
        raise SchemaValidationError("occurred_at must include a UTC offset")
    occurred_at = clock.to_iso(dt)

    payload = data.get("payload", {})
    if not isinstance(payload, dict):
        raise SchemaValidationError(f"payload must be an object, got {type(payload).__name__}")
    _check_json_serializable(payload)

    schema_version = data.get("schema_version")
    if schema_version is not None and (not isinstance(schema_version, int) or schema_version < 1):
        raise SchemaValidationError("schema_version must be a positive integer when present")

    device_id = data.get("device_id") or default_device_id
    if device_id is not None:
        if not isinstance(device_id, str) or not device_id.strip():
            raise SchemaValidationError("device_id must be a non-empty string when present")
        if len(device_id) > _DEVICE_ID_MAX_LEN:
            raise SchemaValidationError(f"device_id exceeds {_DEVICE_ID_MAX_LEN} characters")

    spec = _REGISTRY.get(event_type)
    if spec is None:
        raise UnknownEventTypeError(f"event_type '{event_type}' is not a registered event type")

    effective_version = schema_version if schema_version is not None else spec.schema_version
    if effective_version != spec.schema_version:
        raise UnsupportedVersionError(
            f"event_type '{event_type}' schema_version {effective_version} is not supported "
            f"by this server (expects {spec.schema_version})"
        )

    for field_name, expected_type in spec.required_fields.items():
        if field_name not in payload:
            raise SchemaValidationError(
                f"event_type '{event_type}' payload is missing required field '{field_name}'"
            )
        if not isinstance(payload[field_name], expected_type):
            raise SchemaValidationError(
                f"event_type '{event_type}' payload field '{field_name}' has the wrong type"
            )

    return EventEnvelope(
        event_uid=event_uid, event_type=event_type, occurred_at=occurred_at,
        payload=payload, schema_version=spec.schema_version, device_id=device_id,
    )


def validate_envelope_safe(data, *, default_device_id: str | None = None) -> EventValidationResult:
    """Never raises. Wraps validate_envelope() so a batch of events can
    be validated independently — one bad item is reported and skipped,
    not fatal to the rest (see services/sync_service.py)."""
    event_uid = data.get("event_uid") if isinstance(data, dict) else None
    try:
        envelope = validate_envelope(data, default_device_id=default_device_id)
        return EventValidationResult(ok=True, envelope=envelope, event_uid=envelope.event_uid)
    except ProtocolError as e:
        code = next((c for t, c in _ERROR_CODES.items() if isinstance(e, t)), "malformed")
        return EventValidationResult(ok=False, event_uid=event_uid, error_code=code, error=str(e))
    except Exception as e:   # a genuinely unexpected bug — still never raise here
        logger.error(f"Unexpected error validating event envelope: {e}", exc_info=True)
        return EventValidationResult(ok=False, event_uid=event_uid, error_code="internal_error", error=str(e))


def validate_event_batch(items: list, *, default_device_id: str | None = None) -> list[EventValidationResult]:
    """validate_envelope_safe() over a whole batch, preserving order —
    callers zip this 1:1 against the original `items` list."""
    return [validate_envelope_safe(item, default_device_id=default_device_id) for item in items]


# ── Outgoing ordering guarantee ──────────────────────────────────────────

def validate_change_ordering(changes: list) -> None:
    """
    Defensive check that a 'changes' array this server is about to send
    (the 'events' or 'memory' array inside a /sync response) is strictly
    increasing by 'seq' — the ordering guarantee the whole cursor model
    depends on (see event_service.list_since()/memory_service.list_since()).
    Raises SchemaValidationError on any violation; hitting this means a
    genuine server-side ordering bug, not something to silently paper over.
    """
    last = None
    for item in changes:
        if not isinstance(item, dict) or not isinstance(item.get("seq"), int):
            raise SchemaValidationError(f"change item is missing an integer 'seq': {item!r}")
        seq = item["seq"]
        if last is not None and seq <= last:
            raise SchemaValidationError(f"changes are not strictly increasing by seq: {last} -> {seq}")
        last = seq
