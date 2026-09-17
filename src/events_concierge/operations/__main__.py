"""Command-line entrypoint for production preflight and evidence capture."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from ..secret_files import resolve_env_or_file
from .app_role import rotate_app_role_password
from .canary import CanaryOptions, run_canary
from .config_validation import validate_production_config
from .local_restore import run_local_restore_drill


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        payload, passed = _execute(args)
        rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        sys.stdout.write(rendered)
        if args.output is not None:
            _write_evidence(args.output, rendered)
        return 0 if passed else 1
    except Exception as error:
        # Configuration-validation errors may contain DSNs or secret inputs.  Keep even command
        # failures to their type; detailed dependency logs belong in the protected deployment job.
        sys.stderr.write(
            json.dumps(
                {
                    "status": "failed",
                    "error": f"operation failed closed ({type(error).__name__})",
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 2


def _execute(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    if args.command == "validate-config":
        settings = Settings(_env_file=args.env_file)
        config_report = validate_production_config(
            settings,
            load_provider=not args.structural_only,
            migration_credential_present=_migration_credential_present(args.env_file),
        )
        return config_report.to_dict(), config_report.passed
    if args.command == "canary":
        base_url = args.base_url or os.environ.get("EC_CANARY_BASE_URL")
        if not base_url:
            raise ValueError("canary base URL is required")
        canary_report = run_canary(
            CanaryOptions(
                base_url=base_url,
                expected_release_revision=(
                    args.expected_release_revision or os.environ.get("EC_EXPECTED_RELEASE_REVISION")
                ),
                expected_image_digest=(
                    args.expected_image_digest or os.environ.get("EC_EXPECTED_IMAGE_DIGEST")
                ),
                allow_http=args.allow_http,
                allow_local_mode=args.allow_local_mode,
                require_temporal=not args.allow_temporal_degraded,
                # Session evidence is accepted only through the job secret environment, never argv.
                session_cookie=os.environ.get("EC_CANARY_SESSION_COOKIE"),
                csrf_token=os.environ.get("EC_CANARY_CSRF_TOKEN"),
                timeout_seconds=args.timeout_seconds,
                profile=args.profile,
            )
        )
        return canary_report.to_dict(), canary_report.passed
    if args.command == "local-restore-drill":
        restore_report = run_local_restore_drill(args.project_dir)
        return restore_report.to_dict(), restore_report.passed
    if args.command == "rotate-app-role-password":
        migration_url = resolve_env_or_file("EC_MIGRATION_URL")
        password = resolve_env_or_file("EC_APP_ROLE_PASSWORD")
        if migration_url is None or password is None:
            raise ValueError(
                "EC_MIGRATION_URL and EC_APP_ROLE_PASSWORD are required inline or by *_FILE"
            )
        rotation_report = rotate_app_role_password(migration_url, password)
        return rotation_report.to_dict(), rotation_report.passed
    raise AssertionError("unknown operations command")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m events_concierge.operations",
        description="Fail-closed deployment validation and sanitized release evidence capture.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    config = commands.add_parser("validate-config", help="validate production configuration")
    config.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="read EC_ settings from this file; omit to validate only the process environment",
    )
    config.add_argument(
        "--structural-only",
        action="store_true",
        help="do not import deployment-owned provider code (example/CI mode only)",
    )
    _add_output_argument(config)

    canary = commands.add_parser("canary", help="run a non-mutating HTTP deployment canary")
    canary.add_argument("--base-url", help="HTTPS deployment origin (or EC_CANARY_BASE_URL)")
    canary.add_argument("--expected-release-revision")
    canary.add_argument("--expected-image-digest")
    canary.add_argument("--timeout-seconds", type=float, default=5.0)
    canary.add_argument(
        "--profile",
        choices=("production", "private_google_pilot"),
        default="production",
        help="private_google_pilot checks only the exact private Google sign-in contract",
    )
    canary.add_argument("--allow-http", action="store_true", help="local rehearsal only")
    canary.add_argument("--allow-local-mode", action="store_true", help="local rehearsal only")
    canary.add_argument(
        "--allow-temporal-degraded",
        action="store_true",
        help="local outage rehearsal only; staging/release canaries should require Temporal",
    )
    _add_output_argument(canary)

    restore = commands.add_parser(
        "local-restore-drill",
        help="rehearse pg_dump/restore against an isolated local Compose database",
    )
    restore.add_argument("--project-dir", type=Path, default=Path.cwd())
    _add_output_argument(restore)

    rotation = commands.add_parser(
        "rotate-app-role-password",
        help="rotate the non-owner ec_app password with migration-owner authority",
    )
    _add_output_argument(rotation)
    return parser


def _add_output_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output",
        type=Path,
        help="write the sanitized JSON evidence to a new file (existing files are never replaced)",
    )


def _write_evidence(path: Path, rendered: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as evidence:
        evidence.write(rendered)


def _migration_credential_present(env_file: Path | None) -> bool:
    migration_keys = frozenset({"EC_MIGRATION_URL", "EC_MIGRATION_URL_FILE"})
    if any(os.environ.get(key, "").strip() for key in migration_keys):
        return True
    if env_file is None:
        return False
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        if normalized_key.startswith("export "):
            normalized_key = normalized_key.removeprefix("export ").strip()
        if normalized_key in migration_keys and value.strip().strip("'\""):
            return True
    return False


def default_evidence_path(directory: Path, kind: str) -> Path:
    """Return a collision-resistant evidence filename for external release jobs."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return directory / f"{kind}-{timestamp}.json"


if __name__ == "__main__":
    raise SystemExit(main())
