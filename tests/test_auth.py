from __future__ import annotations

import hmac
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from marketpilot.services import api
from marketpilot.services.auth import (
    AuthConfig,
    AuthConfigurationError,
    AuthMode,
    AuthRole,
    install_auth,
    required_role,
)

TOKENS = {
    "MARKETPILOT_READ_ONLY_TOKEN": "reader-token-000000000000000000000",
    "MARKETPILOT_OPERATOR_TOKEN": "operator-token-0000000000000000000",
    "MARKETPILOT_REVIEWER_TOKEN": "reviewer-token-0000000000000000000",
}
PINNED_CODE_VERSION = "commit:0123456789abcdef"


def protected_config(**overrides: str) -> AuthConfig:
    env = {
        "MARKETPILOT_ENV": "production",
        "MARKETPILOT_CODE_VERSION": PINNED_CODE_VERSION,
        **TOKENS,
        **overrides,
    }
    return AuthConfig.from_env(env)


def auth_app(config: AuthConfig) -> FastAPI:
    app = FastAPI(
        docs_url="/docs" if config.docs_enabled else None,
        openapi_url="/openapi.json" if config.docs_enabled else None,
    )
    install_auth(app, config)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/read")
    def read() -> dict[str, str]:
        return {"status": "read"}

    @app.post("/v1/write")
    def write() -> dict[str, str]:
        return {"status": "written"}

    @app.post("/v1/governance/models/demo/promotions")
    def promote() -> dict[str, str]:
        return {"status": "promoted"}

    return app


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_local_mode_preserves_loopback_demo_without_credentials() -> None:
    config = AuthConfig.from_env({"MARKETPILOT_ENV": "development"})
    client = TestClient(auth_app(config))

    assert config.mode is AuthMode.LOCAL
    assert client.get("/v1/read").status_code == 200
    assert client.post("/v1/write").status_code == 200
    assert client.get("/docs").status_code == 200


def test_protected_mode_requires_all_unique_strong_tokens() -> None:
    with pytest.raises(AuthConfigurationError, match="READ_ONLY_TOKEN"):
        AuthConfig.from_env(
            {
                "MARKETPILOT_ENV": "production",
                "MARKETPILOT_CODE_VERSION": PINNED_CODE_VERSION,
            }
        )
    with pytest.raises(AuthConfigurationError, match="CODE_VERSION"):
        AuthConfig.from_env({"MARKETPILOT_ENV": "production", **TOKENS})
    with pytest.raises(AuthConfigurationError, match="unique"):
        protected_config(MARKETPILOT_OPERATOR_TOKEN=TOKENS["MARKETPILOT_READ_ONLY_TOKEN"])
    with pytest.raises(AuthConfigurationError, match="forbidden"):
        AuthConfig.from_env(
            {
                "MARKETPILOT_ENV": "production",
                "MARKETPILOT_AUTH_MODE": "local",
            }
        )


@pytest.mark.parametrize("missing_variable", list(TOKENS))
def test_real_production_api_startup_fails_when_any_secret_is_missing(
    missing_variable: str,
) -> None:
    env = os.environ.copy()
    env.update(
        {
            "MARKETPILOT_ENV": "production",
            "MARKETPILOT_AUTH_MODE": "production",
            "MARKETPILOT_CODE_VERSION": PINNED_CODE_VERSION,
        }
    )
    env.update(TOKENS)
    env.pop(missing_variable)

    result = subprocess.run(
        [sys.executable, "-c", "import marketpilot.services.api"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert missing_variable in result.stderr
    assert all(token not in result.stderr for token in TOKENS.values())


def test_real_production_api_enforces_roles_and_disables_contract_routes() -> None:
    env = os.environ.copy()
    env.update(
        {
            "MARKETPILOT_ENV": "production",
            "MARKETPILOT_AUTH_MODE": "production",
            "MARKETPILOT_CODE_VERSION": PINNED_CODE_VERSION,
            **TOKENS,
        }
    )
    script = """
import os
from fastapi.testclient import TestClient
from marketpilot.services.api import app

client = TestClient(app, raise_server_exceptions=False)
bearer = lambda variable: {"Authorization": "Bearer " + os.environ[variable]}
assert client.get("/health").status_code == 200
assert client.get("/docs").status_code == 404
assert client.get("/openapi.json").status_code == 404
assert client.get("/v1/models").status_code == 401
assert client.get(
    "/v1/models", headers=bearer("MARKETPILOT_READ_ONLY_TOKEN")
).status_code == 200
assert client.post(
    "/v1/decision/run", headers=bearer("MARKETPILOT_READ_ONLY_TOKEN"), json={}
).status_code == 403
assert client.post(
    "/v1/decision/run", headers=bearer("MARKETPILOT_OPERATOR_TOKEN"), json={}
).status_code != 403
governance = "/v1/governance/models/demo/promotions"
assert client.post(
    governance, headers=bearer("MARKETPILOT_OPERATOR_TOKEN"), json={}
).status_code == 403
assert client.post(
    governance, headers=bearer("MARKETPILOT_REVIEWER_TOKEN"), json={}
).status_code != 403
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert all(token not in result.stderr for token in TOKENS.values())


def test_production_health_is_public_but_docs_and_v1_are_closed() -> None:
    client = TestClient(auth_app(protected_config()))

    assert client.get("/health").status_code == 200
    assert client.get("/docs").status_code == 404
    unauthorized = client.get("/v1/read")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == "Bearer"
    assert unauthorized.headers["cache-control"] == "no-store"
    assert client.get("/v1/read", headers=bearer("wrong-token")).status_code == 401


def test_role_matrix_separates_reads_operations_and_governance() -> None:
    client = TestClient(auth_app(protected_config()))
    reader = bearer(TOKENS["MARKETPILOT_READ_ONLY_TOKEN"])
    operator = bearer(TOKENS["MARKETPILOT_OPERATOR_TOKEN"])
    reviewer = bearer(TOKENS["MARKETPILOT_REVIEWER_TOKEN"])

    assert client.get("/v1/read", headers=reader).status_code == 200
    assert client.get("/v1/read", headers=operator).status_code == 200
    assert client.get("/v1/read", headers=reviewer).status_code == 200

    assert client.post("/v1/write", headers=reader).status_code == 403
    assert client.post("/v1/write", headers=reviewer).status_code == 403
    assert client.post("/v1/write", headers=operator).status_code == 200

    governance_path = "/v1/governance/models/demo/promotions"
    assert client.post(governance_path, headers=reader).status_code == 403
    assert client.post(governance_path, headers=operator).status_code == 403
    assert client.post(governance_path, headers=reviewer).status_code == 200


def test_authentication_compares_every_role_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    config = protected_config()
    original = hmac.compare_digest
    calls = 0

    def counted(left: bytes, right: bytes) -> bool:
        nonlocal calls
        calls += 1
        return original(left, right)

    monkeypatch.setattr(hmac, "compare_digest", counted)
    role = config.authenticate(f"Bearer {TOKENS['MARKETPILOT_READ_ONLY_TOKEN']}")

    assert role is AuthRole.READ_ONLY
    assert calls == 3


def test_every_mutable_api_endpoint_is_enumerated_under_the_role_policy() -> None:
    unsafe_methods = {"post", "put", "patch", "delete"}
    mutable = {
        (method.upper(), path)
        for path, operations in api.app.openapi()["paths"].items()
        for method in operations
        if method in unsafe_methods
    }
    expected = {
        ("POST", "/v1/scenario/session-quality/equity-session"),
        ("POST", "/v1/scenario/session-quality/globex-session"),
        ("POST", "/v1/scenario/session-quality/quote-quality"),
        ("POST", "/v1/scenario/economics/assess"),
        ("POST", "/v1/scenario/collector/run"),
        ("POST", "/v1/scenario/alert-delivery/run"),
        ("POST", "/v1/validation/promotion-gate"),
        ("POST", "/v1/governance/models/{model_id}/challengers"),
        ("POST", "/v1/governance/models/{model_id}/promotions"),
        ("POST", "/v1/governance/models/{model_id}/rollbacks"),
        ("POST", "/v1/attribution/signals"),
        ("POST", "/v1/attribution/tasks/{task_id}/reviews"),
        ("POST", "/v1/events/assess"),
        ("POST", "/v1/alerts/{alert_id}/feedback"),
        ("POST", "/v1/decision/run"),
    }

    assert mutable == expected
    for method, path in mutable:
        expected_role = (
            AuthRole.REVIEWER
            if path.startswith("/v1/governance/")
            else AuthRole.OPERATOR
        )
        assert required_role(method, path) is expected_role


def test_next_proxies_forward_only_caller_authorization_without_role_secrets() -> None:
    web_api = Path(__file__).resolve().parents[1] / "apps" / "web" / "app" / "api"
    sources = "\n".join(path.read_text() for path in web_api.rglob("*.ts"))

    assert "request.headers.get(\"authorization\")" in sources
    assert "upstreamHeaders(request" in sources
    assert "MARKETPILOT_READ_ONLY_TOKEN" not in sources
    assert "MARKETPILOT_OPERATOR_TOKEN" not in sources
    assert "MARKETPILOT_REVIEWER_TOKEN" not in sources
    assert "NEXT_PUBLIC_" not in sources
