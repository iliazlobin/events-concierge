"""Fixture-backed Luma browser adapter scaffold (FR-5.4/5.5).

Live Browserbase/CDP integration remains deliberately outside this offline foundation pending the
owner-run G3 relay-acceptance verification.
"""

from __future__ import annotations

from .scripted_browser import (
    BrowserAcknowledgementLostError,
    ScriptedBrowserRsvpDriver,
    ScriptedBrowserScenario,
)
from .source import LumaSource

__all__ = [
    "BrowserAcknowledgementLostError",
    "LumaSource",
    "ScriptedBrowserRsvpDriver",
    "ScriptedBrowserScenario",
]
