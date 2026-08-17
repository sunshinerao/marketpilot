from __future__ import annotations

import hashlib
import hmac
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, Response


class AuthConfigurationError(RuntimeError):
    """Raised before startup when a protected deployment has unsafe credentials."""


class AuthMode(StrEnum):
    LOCAL = "local"
    SHARED = "shared"
    PRODUCTION = "production"


class AuthRole(StrEnum):
    READ_ONLY = "read-only"
    OPERATOR = "operator"
    REVIEWER = "reviewer"


_TOKEN_ENV_BY_ROLE = {
    AuthRole.READ_ONLY: "MARKETPILOT_READ_ONLY_TOKEN",
    AuthRole.OPERATOR: "MARKETPILOT_OPERATOR_TOKEN",
    AuthRole.REVIEWER: "MARKETPILOT_REVIEWER_TOKEN",
}
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_MIN_TOKEN_LENGTH = 32


@dataclass(frozen=True)
class AuthConfig:
    mode: AuthMode
    code_version: str
    token_digests: tuple[tuple[AuthRole, bytes], ...] = ()

    @property
    def protected(self) -> bool:
        return self.mode is not AuthMode.LOCAL

    @property
    def docs_enabled(self) -> bool:
        return self.mode is AuthMode.LOCAL

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AuthConfig:
        deployment = env.get("MARKETPILOT_ENV", "development").strip().lower()
        configured_mode = env.get("MARKETPILOT_AUTH_MODE", "").strip().lower()
        if configured_mode:
            try:
                mode = AuthMode(configured_mode)
            except ValueError as exc:
                raise AuthConfigurationError(
                    "MARKETPILOT_AUTH_MODE must be local, shared, or production"
                ) from exc
        elif deployment in {"development", "test", "local"}:
            mode = AuthMode.LOCAL
        elif deployment == "production":
            mode = AuthMode.PRODUCTION
        else:
            mode = AuthMode.SHARED

        if mode is AuthMode.LOCAL:
            if deployment not in {"development", "test", "local"}:
                raise AuthConfigurationError(
                    "local authentication bypass is forbidden outside a local deployment"
                )
            return cls(
                mode=mode,
                code_version=env.get(
                    "MARKETPILOT_CODE_VERSION", "development-unpinned"
                ).strip(),
            )

        code_version = env.get("MARKETPILOT_CODE_VERSION", "").strip()
        if (
            not code_version
            or code_version == "development-unpinned"
            or len(code_version) > 120
        ):
            raise AuthConfigurationError(
                "MARKETPILOT_CODE_VERSION must identify the pinned deployed artifact"
            )

        tokens: list[tuple[AuthRole, str]] = []
        for role, variable in _TOKEN_ENV_BY_ROLE.items():
            token = env.get(variable, "")
            if len(token) < _MIN_TOKEN_LENGTH or token != token.strip():
                raise AuthConfigurationError(
                    f"{variable} must be a non-whitespace token of at least "
                    f"{_MIN_TOKEN_LENGTH} characters"
                )
            tokens.append((role, token))

        if len({token for _, token in tokens}) != len(tokens):
            raise AuthConfigurationError("authentication tokens must be unique per role")

        return cls(
            mode=mode,
            code_version=code_version,
            token_digests=tuple(
                (role, hashlib.sha256(token.encode("utf-8")).digest()) for role, token in tokens
            ),
        )

    def authenticate(self, authorization: str | None) -> AuthRole | None:
        token = _bearer_token(authorization)
        if token is None:
            return None

        supplied_digest = hashlib.sha256(token.encode("utf-8")).digest()
        matched: AuthRole | None = None
        # Compare against every configured role on every request. Do not short-circuit
        # when a match is found, and never compare variable-length secret strings.
        for role, expected_digest in self.token_digests:
            if hmac.compare_digest(supplied_digest, expected_digest):
                matched = role
        return matched


def install_auth(app: FastAPI, config: AuthConfig) -> None:
    @app.middleware("http")
    async def authorize(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        required = required_role(request.method, request.url.path)
        if not config.protected or required is None:
            return await call_next(request)

        role = config.authenticate(request.headers.get("authorization"))
        if role is None:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "authentication required"},
                headers={
                    "WWW-Authenticate": "Bearer",
                    "Cache-Control": "no-store",
                },
            )
        if not role_allows(role, required):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "insufficient permissions"},
                headers={"Cache-Control": "no-store"},
            )
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        return response


def required_role(method: str, path: str) -> AuthRole | None:
    """Return the minimum role for API ingress; health is intentionally public."""

    if path == "/health" or not path.startswith("/v1"):
        return None
    if method.upper() in _SAFE_METHODS:
        return AuthRole.READ_ONLY
    if path == "/v1/governance" or path.startswith("/v1/governance/"):
        return AuthRole.REVIEWER
    return AuthRole.OPERATOR


def role_allows(actual: AuthRole, required: AuthRole) -> bool:
    if required is AuthRole.READ_ONLY:
        return True
    return actual is required


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token:
        return None
    if token != token.strip() or any(character.isspace() for character in token):
        return None
    return token
