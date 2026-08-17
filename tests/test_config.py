from pathlib import Path

import pytest

from marketpilot.config import load_rules


def test_rules_are_loaded_from_versioned_config() -> None:
    rules = load_rules(Path("config/rules-v1.toml"))
    assert rules.version == "rules-v1"
    assert rules.strike_increment == 5
    assert rules.freshness_seconds.option_chain == 5
    assert rules.risk.p0_quantile == 0.99
    assert rules.sessions.preopen_cutoff_et == "09:29:59"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('timezone = "America/New_York"', 'timezone = "UTC"', "timezone"),
        ("wing_width = 5", "wing_width = 7", "align"),
        ("normal_quantile = 0.95", "normal_quantile = 1.1", "quantiles"),
        ("futures = 2", "futures = 0", "freshness"),
    ],
)
def test_invalid_runtime_rules_fail_closed(
    tmp_path: Path,
    old: str,
    new: str,
    message: str,
) -> None:
    content = Path("config/rules-v1.toml").read_text().replace(old, new)
    candidate = tmp_path / "rules.toml"
    candidate.write_text(content)

    with pytest.raises(ValueError, match=message):
        load_rules(candidate)
