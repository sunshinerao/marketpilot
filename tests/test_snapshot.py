from datetime import UTC, datetime

from marketpilot.domain.snapshot import freeze_snapshot


def test_snapshot_hash_is_order_independent_and_deterministic() -> None:
    as_of = datetime(2026, 8, 17, 13, 45, tzinfo=UTC)
    first = freeze_snapshot({"as_of": as_of, "center": 7812.4, "contract": "ESU6"})
    second = freeze_snapshot({"contract": "ESU6", "center": 7812.4, "as_of": as_of})
    assert first == second
