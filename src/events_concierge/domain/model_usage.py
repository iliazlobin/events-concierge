"""Non-textual accounting for one physical model request, in provider-reported USD."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

ModelCallStatus = Literal["ok", "failed", "interrupted"]
_MAX_MONEY = 1_000_000
_MAX_LABEL_LENGTH = 200
_PRINTABLE_START, _PRINTABLE_END = 0x21, 0x7F


@dataclass(frozen=True)
class ModelCallUsage:
    status: ModelCallStatus
    actual_model: str | None = None
    generation_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: Decimal | None = None
    error_code: str | None = None

    def as_json(self) -> dict[str, Any]:
        result = asdict(self)
        result["cost_usd"] = str(self.cost_usd) if self.cost_usd is not None else None
        return result


def reported_money(value: object) -> Decimal | None:
    """Missing, non-finite and negative costs are unknown; an explicit zero is valid."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and 0 <= amount <= _MAX_MONEY else None


def _tokens(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**9
        else None
    )


def _label(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_LABEL_LENGTH:
        return None
    return value if all(_PRINTABLE_START <= ord(char) < _PRINTABLE_END for char in value) else None


def response_usage(payload: dict[str, Any]) -> ModelCallUsage:
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    prompt = usage.get("prompt_tokens_details")
    completion = usage.get("completion_tokens_details")
    choices = payload.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    failed = bool(payload.get("error")) or not isinstance(choice, dict)
    if isinstance(choice, dict):
        failed = (
            failed
            or bool(choice.get("error"))
            or choice.get("finish_reason") == "error"
            or not isinstance(choice.get("message"), dict)
        )
    return ModelCallUsage(
        status="failed" if failed else "ok",
        actual_model=_label(payload.get("model")),
        generation_id=_label(payload.get("id")),
        input_tokens=_tokens(usage.get("prompt_tokens")),
        output_tokens=_tokens(usage.get("completion_tokens")),
        cached_tokens=_tokens(prompt.get("cached_tokens")) if isinstance(prompt, dict) else None,
        reasoning_tokens=_tokens(completion.get("reasoning_tokens"))
        if isinstance(completion, dict)
        else None,
        cost_usd=reported_money(usage.get("cost")),
        error_code="provider_response_error" if failed else None,
    )


class ModelUsageError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
