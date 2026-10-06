"""Provider provisioning stays read-only by default and never reports provider credentials."""

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_PATH = Path(__file__).parents[2] / "scripts" / "identity_platform.py"
_SPEC = importlib.util.spec_from_file_location("identity_platform_bootstrap", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bootstrap)

_GOOGLE = {"client_id": "reviewed-google-client", "client_secret": "private-google-secret"}
_APPLE = {
    "client_id": "reviewed-apple-service",
    "team_id": "123456789A",
    "key_id": "ABCDEFGHIJ",
    "private_key": "-----BEGIN PRIVATE KEY-----\nprivate-apple-key\n-----END PRIVATE KEY-----",
}


@pytest.mark.parametrize("provider,source", [("google", _GOOGLE), ("apple", _APPLE)])
def test_dry_run_validates_secret_but_performs_no_mutation_or_secret_output(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    source: dict[str, str],
) -> None:
    calls: list[Any] = []

    def gcloud(args: list[str]) -> bytes:
        assert "--quiet" not in args  # _gcloud adds it; callers never pass a shell or secret.
        return (
            json.dumps(source).encode() if args[:2] == ["secrets", "versions"] else b"private-token"
        )

    def request(project: str, token: str, method: str, url: str, payload: Any = None) -> Any:
        calls.append((method, url))
        assert token == "private-token" and payload is None
        return 404, {}

    monkeypatch.setattr(bootstrap, "_gcloud", gcloud)
    monkeypatch.setattr(bootstrap, "_request", request)
    result = bootstrap.run(
        project="events-identity-test", provider=provider, secret_version="3", apply=False
    )
    assert len(calls) == 1 and calls[0][0] == "GET"
    assert not result["applied"]
    assert result["requires_browser_acceptance"]
    for secret in (*source.values(), "private-token"):
        assert secret not in json.dumps(result)


def test_apple_patch_delivers_code_flow_configuration_and_verifies_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(
        bootstrap,
        "_gcloud",
        lambda args: (
            json.dumps(_APPLE).encode() if args[:2] == ["secrets", "versions"] else b"private-token"
        ),
    )

    def request(project: str, token: str, method: str, url: str, payload: Any = None) -> Any:
        calls.append((method, url, payload))
        return 200, {"clientId": _APPLE["client_id"], "enabled": True}

    monkeypatch.setattr(bootstrap, "_request", request)
    result = bootstrap.run(
        project="events-identity-test", provider="apple", secret_version="4", apply=True
    )
    assert [call[0] for call in calls] == ["GET", "PATCH", "GET"]
    assert calls[1][2]["appleSignInConfig"]["codeFlowConfig"] == {
        "teamId": _APPLE["team_id"],
        "keyId": _APPLE["key_id"],
        "privateKey": _APPLE["private_key"],
    }
    assert "updateMask=enabled%2CclientId%2CappleSignInConfig" in calls[1][1]
    assert result["applied"] and result["client_matches"]


@pytest.mark.parametrize(
    "source",
    [None, {**_GOOGLE, "unexpected": "private"}, {"client_id": "client", "client_secret": ""}],
)
def test_invalid_secret_never_reaches_provider_http(source: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        bootstrap.provider_payload("google", source)


def test_secret_version_and_project_cannot_change_the_command_or_request_authority() -> None:
    for project, version in [
        ("events-identity-test", "latest"),
        ("x;changed", "1"),
        ("events-identity-test", "--account=attacker"),
    ]:
        with pytest.raises(ValueError):
            bootstrap.run(project=project, provider="google", secret_version=version, apply=True)
