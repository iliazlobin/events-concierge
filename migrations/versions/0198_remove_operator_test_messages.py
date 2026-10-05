"""Remove test-message recognition from operator record projections.

Revision ID: 0198
Revises: 0197

All request errors use the same classification. Exact stored messages remain
available through the existing diagnostic read. No queue data or grants change.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from runpy import run_path

revision: str = "0198"
down_revision: str | None = "0197"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_between(body: str, start: str, end: str, replacement: str) -> str:
    if body.count(start) != 1 or body.count(end) != 1:
        raise RuntimeError("operator projection differs from its migration contract")
    first = body.index(start)
    last = body.index(end, first)
    return body[:first] + replacement + body[last:]


_records = run_path(str(Path(__file__).with_name("0189_operator_signed_work_references.py")))
_replace_projection = _records["_replace_projection"]
DOWNGRADE_RECORDS_SQL: str = _records["UPGRADE_SQL"]
DOWNGRADE_ERRORS_SQL: str = run_path(
    str(Path(__file__).with_name("0185_operator_error_diagnostics.py"))
)["_PROJECTION"].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)

# Reuse the immutable definitions to preserve queue scope, signed IDs, and
# redaction while removing message-dependent request classification and verbose fallback copy.
UPGRADE_RECORDS_SQL = _replace_between(
    DOWNGRADE_RECORDS_SQL,
    "                 WHEN p_queue = 'request_start' THEN\n",
    "                 ELSE CASE last_error\n",
    "                 WHEN p_queue = 'request_start' THEN 'unclassified'\n",
)
UPGRADE_RECORDS_SQL = (
    _replace_between(
        UPGRADE_RECORDS_SQL,
        "                WHEN 'test_retry_fixture'",
        "                WHEN 'delivery_failed'",
        "",
    )
    .replace(
        "An error was recorded. Raw error text is unavailable in this view.", "Error recorded."
    )
    .replace("No failure reason was recorded.", "No error message recorded.")
)
UPGRADE_ERRORS_SQL = _replace_between(
    DOWNGRADE_ERRORS_SQL,
    "                        ELSE 'ready' END AS state,\n",
    "            FROM public.request_start_outbox",
    "                        ELSE 'ready' END AS state,\n"
    "                   'unclassified'::text AS error_code,\n"
    "                   'Error recorded.'::text AS error_summary\n",
)


def upgrade() -> None:
    _replace_projection(UPGRADE_RECORDS_SQL)
    _replace_projection(UPGRADE_ERRORS_SQL)


def downgrade() -> None:
    _replace_projection(DOWNGRADE_RECORDS_SQL)
    _replace_projection(DOWNGRADE_ERRORS_SQL)
