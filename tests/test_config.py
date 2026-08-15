from pathlib import Path

from marketpilot.config import load_rules


def test_rules_are_loaded_from_versioned_config() -> None:
    rules = load_rules(Path("config/rules-v1.toml"))
    assert rules.version == "rules-v1"
    assert rules.strike_increment == 5
    assert rules.freshness_seconds.option_chain == 5
