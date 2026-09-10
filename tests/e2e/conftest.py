"""Browser harness for the same-origin consumer product.

The real FastAPI process serves the committed shell and assets. Tests intercept only JSON API
requests in Chromium, which keeps product behavior deterministic without replacing the HTML,
CSS, JavaScript, CSP, or HTTP delivery path under test.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from playwright.sync_api import Browser, BrowserContext, Error, Page, sync_playwright

_PROJECT_ROOT = Path(__file__).parents[2]
_SESSION_KEY = "events-concierge.local-session.v1"
_TENANT_ID = "11111111-1111-4111-8111-111111111111"


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_server(process: subprocess.Popen[str], url: str, log: Path) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(
                f"consumer test server exited with {process.returncode}:\n{log.read_text()}",
                pytrace=False,
            )
        try:
            with urlopen(f"{url}/healthz", timeout=0.25) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            time.sleep(0.05)
    pytest.fail(f"consumer test server did not become ready:\n{log.read_text()}", pytrace=False)


@pytest.fixture(scope="session")
def product_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Run the production ASGI app without requiring its service dependencies."""
    port = _unused_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    log = tmp_path_factory.mktemp("consumer-browser-server") / "uvicorn.log"
    environment = os.environ.copy()
    environment.update(
        {
            "EC_ADMIN_INGESTION_ENABLED": "true",
            # The admin shell still constructs its dedicated pool. Browser routes replace all
            # evidence requests; closed loopback ports prevent an accidental runtime DB read.
            "EC_DATABASE_URL": "postgresql+psycopg://ec_app:fixture@127.0.0.1:1/browser_fixture",
            "EC_OPERATOR_DATABASE_URL": "postgresql+psycopg://ec_browser_operator:fixture@127.0.0.1:1/browser_fixture",
            "EC_PACER_BACKEND": "memory",
            "EC_REDIS_URL": "redis://127.0.0.1:1/0",
            "EC_LOG_LEVEL": "warning",
            "EC_MOCK_CLOUD": "true",
            "EC_TEMPORAL_TARGET": "127.0.0.1:1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    with log.open("w+", encoding="utf-8") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "events_concierge.api.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=_PROJECT_ROOT,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_for_server(process, base_url, log)
            yield base_url
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    """Launch the pinned Chromium build, with an opt-in system Chrome escape hatch."""
    playwright = sync_playwright().start()
    channel = os.environ.get("EC_E2E_BROWSER_CHANNEL")
    launch_options: dict[str, object] = {"headless": True}
    if channel:
        launch_options["channel"] = channel
    try:
        instance = playwright.chromium.launch(**launch_options)
    except Error as error:
        playwright.stop()
        pytest.fail(
            "Chromium could not start. Run `make browser-install`, or set "
            "EC_E2E_BROWSER_CHANNEL=chrome to use local Chrome.\n"
            f"{error}",
            pytrace=False,
        )
    try:
        yield instance
    finally:
        instance.close()
        playwright.stop()


@dataclass(slots=True)
class BrowserPage:
    context: BrowserContext
    page: Page
    page_errors: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    allowed_console_error_fragments: list[str] = field(default_factory=list)


PageFactory = Callable[..., BrowserPage]


@pytest.fixture
def page_factory(browser: Browser) -> Iterator[PageFactory]:
    """Create isolated pages and fail the owning test on browser/runtime errors."""
    opened: list[BrowserPage] = []

    def create(
        *,
        width: int = 1440,
        height: int = 1000,
        authenticated: bool = False,
        reduced_motion: Literal["no-preference", "reduce"] = "no-preference",
        probe_scroll: bool = False,
    ) -> BrowserPage:
        context = browser.new_context(
            viewport={"width": width, "height": height},
            locale="en-US",
            timezone_id="America/Los_Angeles",
            reduced_motion=reduced_motion,
        )
        if authenticated:
            context.add_init_script(
                script=(
                    "try { localStorage.setItem("
                    f"{_SESSION_KEY!r}, JSON.stringify({{ tenantId: {_TENANT_ID!r} }})); "
                    "} catch (_error) {}"
                )
            )
        if probe_scroll:
            context.add_init_script(
                script="""
                    window.__e2eScrollCalls = [];
                    window.scrollTo = (...args) => {
                      const call = args.length === 1
                        ? args[0]
                        : { left: args[0], top: args[1] };
                      window.__e2eScrollCalls.push(call);
                    };
                """
            )
        page = context.new_page()
        harness = BrowserPage(context=context, page=page)
        page.on("pageerror", lambda error: harness.page_errors.append(str(error)))
        page.on(
            "console",
            lambda message: (
                harness.console_errors.append(message.text) if message.type == "error" else None
            ),
        )
        opened.append(harness)
        return harness

    yield create

    failures: list[str] = []
    for harness in opened:
        if not harness.page.is_closed():
            harness.page.wait_for_timeout(50)
        failures.extend(f"page error: {value}" for value in harness.page_errors)
        allowed = list(harness.allowed_console_error_fragments)
        for value in harness.console_errors:
            match = next((item for item in allowed if item in value), None)
            if match is None:
                failures.append(f"console error: {value}")
            else:
                allowed.remove(match)
        failures.extend(f"expected console error was not emitted: {value}" for value in allowed)
        harness.context.close()
    assert not failures, "Browser runtime emitted errors:\n" + "\n".join(failures)
