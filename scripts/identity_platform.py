"""Configure one Google/Apple provider from a pinned Secret Manager version, without token output.

By default only read, validate and report the public provider configuration. --apply is an
explicit provisioning action; provider secrets are delivered to Identity Platform, never Terraform.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_MAX_PAYLOAD_BYTES = 32 * 1024
_PROVIDERS = {"google": "google.com", "apple": "apple.com"}


def provider_payload(provider: str, source: Mapping[str, Any]) -> dict[str, Any]:
    """Reject accidental extra fields and avoid secret-bearing validation errors."""
    expected = (
        {"client_id", "client_secret"}
        if provider == "google"
        else {
            "client_id",
            "team_id",
            "key_id",
            "private_key",
        }
    )
    if (
        provider not in _PROVIDERS
        or set(source) != expected
        or any(
            not isinstance(value, str)
            or not value.strip()
            or len(value.encode()) > _MAX_PAYLOAD_BYTES
            for value in source.values()
        )
    ):
        raise ValueError("provider secret has an invalid schema")
    payload: dict[str, Any] = {"enabled": True, "clientId": source["client_id"]}
    if provider == "google":
        payload["clientSecret"] = source["client_secret"]
    else:
        if (
            not re.fullmatch(r"[A-Z0-9]{10}", source["team_id"])
            or not re.fullmatch(r"[A-Z0-9]{10}", source["key_id"])
            or not source["private_key"].startswith("-----BEGIN PRIVATE KEY-----")
        ):
            raise ValueError("Apple developer credentials have an invalid schema")
        payload["appleSignInConfig"] = {
            "codeFlowConfig": {
                "teamId": source["team_id"],
                "keyId": source["key_id"],
                "privateKey": source["private_key"],
            }
        }
    return payload


def _gcloud(args: list[str]) -> bytes:
    process = subprocess.run(
        ["gcloud", *args, "--quiet"], capture_output=True, check=False, timeout=30
    )
    if process.returncode or len(process.stdout) > _MAX_PAYLOAD_BYTES:
        # CLI errors could contain provider credentials; never print captured output.
        raise ValueError("gcloud identity/secret access failed; inspect access separately")
    return process.stdout


def _request(
    project: str, access_token: str, method: str, url: str, payload: Mapping[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    request = Request(
        url,
        method=method,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "X-Goog-User-Project": project,
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            raw = response.read(_MAX_PAYLOAD_BYTES + 1)
            if len(raw) > _MAX_PAYLOAD_BYTES:
                raise ValueError("identity service response exceeded its bound")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("identity service response is invalid")
            return response.status, body
    except HTTPError as error:
        if method == "GET" and error.code == HTTPStatus.NOT_FOUND:
            return HTTPStatus.NOT_FOUND, {}
        raise ValueError(f"identity configuration request failed with HTTP {error.code}") from None
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise ValueError("identity configuration service is unavailable") from error


def run(*, project: str, provider: str, secret_version: str, apply: bool) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project) or provider not in _PROVIDERS:
        raise ValueError("use a reviewed project and Google or Apple provider")
    if not re.fullmatch(r"[1-9][0-9]*", secret_version):
        raise ValueError("pin a numbered secret version")
    source = _gcloud(
        [
            "secrets",
            "versions",
            "access",
            secret_version,
            "--secret",
            f"ec-consumer-{provider}",
            "--project",
            project,
        ]
    )
    try:
        parsed = json.loads(source)
        if not isinstance(parsed, dict):
            raise ValueError
        payload = provider_payload(provider, parsed)
    except (ValueError, TypeError) as error:
        raise ValueError("provider secret has an invalid schema") from error
    token = _gcloud(["auth", "print-access-token"]).decode().strip()
    if not token:
        raise ValueError("an authenticated GCP operator is required")
    root = (
        f"https://identitytoolkit.googleapis.com/v2/projects/{project}/defaultSupportedIdpConfigs"
    )
    endpoint = f"{root}/{_PROVIDERS[provider]}"
    status, current = _request(project, token, "GET", endpoint)
    if apply:
        if status == HTTPStatus.NOT_FOUND:
            _request(
                project,
                token,
                "POST",
                root + "?" + urlencode({"idpId": _PROVIDERS[provider]}),
                payload,
            )
        else:
            mask = ",".join(payload)
            _request(
                project, token, "PATCH", endpoint + "?" + urlencode({"updateMask": mask}), payload
            )
        status, current = _request(project, token, "GET", endpoint)
        if (
            status != HTTPStatus.OK
            or current.get("clientId") != payload["clientId"]
            or current.get("enabled") is not True
        ):
            raise ValueError("provider configuration readback did not match")
    return {
        "project": project,
        "provider": _PROVIDERS[provider],
        "secret_version": secret_version,
        "configured": status == HTTPStatus.OK,
        "enabled": current.get("enabled") is True,
        "client_matches": current.get("clientId") == payload["clientId"],
        "applied": apply,
        "requires_browser_acceptance": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--provider", choices=tuple(_PROVIDERS), required=True)
    parser.add_argument("--secret-version", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                run(
                    project=args.project,
                    provider=args.provider,
                    secret_version=args.secret_version,
                    apply=args.apply,
                ),
                sort_keys=True,
            )
        )
    except (ValueError, subprocess.TimeoutExpired):
        parser.exit(
            1, "Provider configuration failed; inspect access and the pinned secret schema.\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
