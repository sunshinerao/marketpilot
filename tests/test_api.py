from marketpilot.services.api import health, model_health, models


def test_health_and_model_registry_are_exposed() -> None:
    assert health()["status"] == "ok"
    registered = models()
    assert registered[0]["model_id"] == "strikepilot_spxw_0dte_ic"


def test_model_health_is_explicitly_not_calibrated() -> None:
    assert model_health()["status"] == "NOT_CALIBRATED"
