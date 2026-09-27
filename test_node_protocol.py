"""
test_node_protocol.py
Focused tests for MayaNode's own protocol implementation (node/protocol.py)
and its wiring into api/sync.py + services/sync_service.py: schema
validation, ordering/cursors, deduplication, versioning, and malformed
payloads. Stdlib unittest only.

Per docs/PROTOCOL_CONTRACT.md, MayaNode and MayaVE are separate
repositories with independent implementations of the same wire shape —
this file tests ONLY the MayaNode (server) side. See
test_mayave_protocol.py for the MayaVE (client) side, and
test_protocol_compatibility.py for a contract test that exercises both.
"""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from node.protocol import (
    EventEnvelope, ProtocolError, SchemaValidationError, UnknownEventTypeError,
    UnsupportedVersionError, PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS,
    is_version_supported, register_event_type, registered_event_types,
    stamp_protocol_version, validate_change_ordering, validate_envelope,
    validate_envelope_safe, validate_event_batch,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# A test-only event type, registered once for this module.
register_event_type("test.sample", schema_version=1, required_fields={"note": str})


class EnvelopeValidationTests(unittest.TestCase):
    def test_valid_envelope_round_trips(self):
        raw = {
            "event_uid": "evt-1", "event_type": "test.sample",
            "occurred_at": _now_iso(), "payload": {"note": "hello"},
        }
        envelope = validate_envelope(raw)
        self.assertIsInstance(envelope, EventEnvelope)
        self.assertEqual(envelope.event_type, "test.sample")
        self.assertEqual(envelope.schema_version, 1)
        self.assertEqual(envelope.payload, {"note": "hello"})

    def test_missing_event_uid_rejected(self):
        raw = {"event_type": "test.sample", "occurred_at": _now_iso(), "payload": {"note": "x"}}
        with self.assertRaises(SchemaValidationError):
            validate_envelope(raw)

    def test_malformed_event_type_rejected(self):
        raw = {
            "event_uid": "evt-2", "event_type": "NotNamespaced",
            "occurred_at": _now_iso(), "payload": {"note": "x"},
        }
        with self.assertRaises(SchemaValidationError):
            validate_envelope(raw)

    def test_missing_utc_offset_rejected(self):
        raw = {
            "event_uid": "evt-3", "event_type": "test.sample",
            "occurred_at": "2024-01-01T00:00:00", "payload": {"note": "x"},
        }
        with self.assertRaises(SchemaValidationError):
            validate_envelope(raw)

    def test_non_json_serialisable_payload_rejected(self):
        raw = {
            "event_uid": "evt-4", "event_type": "test.sample",
            "occurred_at": _now_iso(), "payload": {"note": {1, 2, 3}},
        }
        with self.assertRaises(SchemaValidationError):
            validate_envelope(raw)

    def test_missing_required_field_rejected(self):
        raw = {
            "event_uid": "evt-5", "event_type": "test.sample",
            "occurred_at": _now_iso(), "payload": {},
        }
        with self.assertRaises(SchemaValidationError):
            validate_envelope(raw)

    def test_wrong_typed_required_field_rejected(self):
        raw = {
            "event_uid": "evt-6", "event_type": "test.sample",
            "occurred_at": _now_iso(), "payload": {"note": 123},
        }
        with self.assertRaises(SchemaValidationError):
            validate_envelope(raw)

    def test_unknown_event_type_rejected(self):
        raw = {
            "event_uid": "evt-7", "event_type": "nope.unregistered",
            "occurred_at": _now_iso(), "payload": {},
        }
        with self.assertRaises(UnknownEventTypeError):
            validate_envelope(raw)

    def test_unsupported_schema_version_rejected(self):
        raw = {
            "event_uid": "evt-8", "event_type": "test.sample", "schema_version": 99,
            "occurred_at": _now_iso(), "payload": {"note": "x"},
        }
        with self.assertRaises(UnsupportedVersionError):
            validate_envelope(raw)

    def test_default_device_id_applied_when_absent(self):
        raw = {
            "event_uid": "evt-9", "event_type": "test.sample",
            "occurred_at": _now_iso(), "payload": {"note": "x"},
        }
        envelope = validate_envelope(raw, default_device_id="device-x")
        self.assertEqual(envelope.device_id, "device-x")

    def test_dedup_key_combines_device_and_event_uid(self):
        raw = {
            "event_uid": "evt-11", "event_type": "test.sample", "device_id": "device-a",
            "occurred_at": _now_iso(), "payload": {"note": "x"},
        }
        envelope = validate_envelope(raw)
        self.assertEqual(envelope.dedup_key(), ("device-a", "evt-11"))


class SafeValidationTests(unittest.TestCase):
    def test_safe_validation_never_raises_on_garbage(self):
        garbage_inputs = [
            {}, {"event_uid": 123}, {"event_uid": "x", "event_type": None},
            "not a dict", None, 42,
            {"event_uid": "x", "event_type": "test.sample", "occurred_at": "not-a-date", "payload": {}},
        ]
        for item in garbage_inputs:
            with self.subTest(item=item):
                result = validate_envelope_safe(item)
                self.assertFalse(result.ok)
                self.assertIsNotNone(result.error_code)

    def test_batch_reports_one_rejection_without_losing_the_rest(self):
        good = {
            "event_uid": "evt-ok", "event_type": "test.sample",
            "occurred_at": _now_iso(), "payload": {"note": "fine"},
        }
        bad = {
            "event_uid": "evt-bad", "event_type": "test.sample",
            "occurred_at": _now_iso(), "payload": {},   # missing 'note'
        }
        results = validate_event_batch([good, bad])
        self.assertTrue(results[0].ok)
        self.assertFalse(results[1].ok)
        self.assertEqual(results[1].error_code, "malformed")


class EventTypeRegistryTests(unittest.TestCase):
    def test_reregistering_identical_spec_is_a_noop(self):
        register_event_type("test.sample", schema_version=1, required_fields={"note": str})
        self.assertIn("test.sample", registered_event_types())

    def test_reregistering_conflicting_spec_raises(self):
        with self.assertRaises(ProtocolError):
            register_event_type("test.sample", schema_version=2, required_fields={"note": str})

    def test_builtin_ping_type_is_registered(self):
        types = registered_event_types()
        self.assertIn("protocol.ping", types)
        self.assertEqual(types["protocol.ping"].required_fields, {})


class VersionTests(unittest.TestCase):
    def test_current_version_is_supported(self):
        self.assertTrue(is_version_supported(PROTOCOL_VERSION))
        self.assertIn(PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS)

    def test_unsupported_version_rejected(self):
        self.assertFalse(is_version_supported(999))

    def test_garbage_version_is_not_supported_and_does_not_raise(self):
        self.assertFalse(is_version_supported("not-a-number"))
        self.assertFalse(is_version_supported(None))

    def test_stamp_protocol_version_sets_current_version(self):
        response = stamp_protocol_version({"cursor": 1})
        self.assertEqual(response["protocol_version"], PROTOCOL_VERSION)


class OrderingTests(unittest.TestCase):
    def test_validate_change_ordering_accepts_strictly_increasing(self):
        validate_change_ordering([{"seq": 1}, {"seq": 2}, {"seq": 5}])   # must not raise

    def test_validate_change_ordering_rejects_non_increasing(self):
        with self.assertRaises(SchemaValidationError):
            validate_change_ordering([{"seq": 2}, {"seq": 2}])

    def test_validate_change_ordering_rejects_missing_seq(self):
        with self.assertRaises(SchemaValidationError):
            validate_change_ordering([{"no_seq": True}])


# ══════════════════════════════════════════════════════════════════════
# services/sync_service.py integration — real (temporary) SQLite DB
# ══════════════════════════════════════════════════════════════════════

class SyncServiceProtocolIntegrationTests(unittest.TestCase):
    """
    Exercises the protocol layer wired into services/sync_service.py
    against a real, temporary SQLite database. node.config.DB_PATH is
    monkeypatched per test rather than via environment variable, because
    database/connection.py reads config.DB_PATH fresh on every call —
    safe regardless of what other test modules imported first.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmpdir.name) / "test.db"
        self._patcher = patch("node.config.DB_PATH", self._db_path)
        self._patcher.start()

        import database.migrate as migrate
        migrate.run()

        import services.sync_service as sync_service
        self.sync_service = sync_service

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_idempotent_event_replay_does_not_duplicate(self):
        event = {
            "event_uid": "evt-dup", "event_type": "test.sample",
            "occurred_at": _now_iso(), "payload": {"note": "once"}, "device_id": "device-1",
        }
        first = self.sync_service.apply_sync("device-1", 0, [event], [], limit=100)
        second = self.sync_service.apply_sync("device-1", 0, [event], [], limit=100)

        self.assertEqual(first["accepted_events"][0]["status"], "accepted")
        self.assertEqual(second["accepted_events"][0]["status"], "duplicate")
        self.assertEqual(
            first["accepted_events"][0]["seq"], second["accepted_events"][0]["seq"],
        )

    def test_malformed_event_is_rejected_without_losing_valid_siblings(self):
        good = {
            "event_uid": "evt-good", "event_type": "test.sample",
            "occurred_at": _now_iso(), "payload": {"note": "fine"}, "device_id": "device-1",
        }
        bad = {
            "event_uid": "evt-missing-field", "event_type": "test.sample",
            "occurred_at": _now_iso(), "payload": {}, "device_id": "device-1",
        }
        result = self.sync_service.apply_sync("device-1", 0, [good, bad], [], limit=100)
        statuses = {r["event_uid"]: r["status"] for r in result["accepted_events"]}
        self.assertEqual(statuses["evt-good"], "accepted")
        self.assertEqual(statuses["evt-missing-field"], "rejected")
        self.assertEqual(
            next(r for r in result["accepted_events"] if r["event_uid"] == "evt-missing-field")["error_code"],
            "malformed",
        )

    def test_unknown_event_type_is_rejected(self):
        bad = {
            "event_uid": "evt-unknown-type", "event_type": "nope.unregistered",
            "occurred_at": _now_iso(), "payload": {}, "device_id": "device-1",
        }
        result = self.sync_service.apply_sync("device-1", 0, [bad], [], limit=100)
        self.assertEqual(result["accepted_events"][0]["status"], "rejected")
        self.assertEqual(result["accepted_events"][0]["error_code"], "unknown_event_type")

    def test_response_carries_protocol_version(self):
        result = self.sync_service.apply_sync("device-1", 0, [], [], limit=100)
        self.assertEqual(result["protocol_version"], PROTOCOL_VERSION)

    def test_cursor_ordering_is_replayable(self):
        events = [
            {
                "event_uid": f"evt-{i}", "event_type": "test.sample",
                "occurred_at": _now_iso(), "payload": {"note": str(i)}, "device_id": "device-1",
            }
            for i in range(3)
        ]
        first = self.sync_service.apply_sync("device-1", 0, events, [], limit=100)
        cursor_after_first = first["cursor"]
        validate_change_ordering(first["changes"]["events"])

        # A second device joining later, syncing from 0, must replay the
        # same events in the same strictly-increasing order.
        replay = self.sync_service.apply_sync("device-2", 0, [], [], limit=100)
        validate_change_ordering(replay["changes"]["events"])
        self.assertEqual(replay["cursor"], cursor_after_first)


if __name__ == "__main__":
    unittest.main()
