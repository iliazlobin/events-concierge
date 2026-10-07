"""Exercise auth headers through the built Next server and a loopback-only API fixture."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

_WEB = Path(__file__).resolve().parents[2] / "web"
pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(
        not (_WEB / ".next/BUILD_ID").is_file(), reason="Build Next before this HTTP gate"
    ),
]


@pytest.fixture(scope="module")
def auth_header_server(tmp_path_factory: pytest.TempPathFactory):
    class FixtureApi(BaseHTTPRequestHandler):
        def respond(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Type", "application/json")
            if status == 200:
                self.send_header(
                    "Set-Cookie", "__Host-ec_login=fixture; Path=/; Secure; HttpOnly; SameSite=Lax"
                )
            self.end_headers()
            self.wfile.write(b'{"detail":"fixture response"}')

        def do_GET(self) -> None:
            self.respond(200)

        def do_POST(self) -> None:
            self.respond(401)

        def log_message(self, format: str, *args: object) -> None:
            pass

    backend = ThreadingHTTPServer(("127.0.0.1", 0), FixtureApi)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    node = shutil.which("node")
    assert node is not None
    environment = {
        key: value for key, value in os.environ.items() if key in {"PATH", "TMPDIR", "LANG"}
    }
    environment.update(
        {
            "NODE_ENV": "production",
            "NEXT_TELEMETRY_DISABLED": "1",
            "EC_API_ORIGIN": f"http://127.0.0.1:{backend.server_port}",
        }
    )
    folder = tmp_path_factory.mktemp("next-auth-headers")
    log = folder / "server.log"
    base = f"http://127.0.0.1:{port}"
    try:
        with log.open("w") as output:
            process = subprocess.Popen(
                [
                    node,
                    "node_modules/next/dist/bin/next",
                    "start",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=_WEB,
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    assert process.poll() is None, "isolated Next server exited"
                    try:
                        with urlopen(f"{base}/auth/identity/start", timeout=1) as response:
                            assert response.status == 200
                            break
                    except (OSError, URLError):
                        time.sleep(0.1)
                else:
                    pytest.fail("isolated Next server did not become ready")
                yield base
            finally:
                process.terminate()
                process.wait(timeout=10)
    finally:
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    "method,path,status",
    [("GET", "/auth/identity/start", 200), ("POST", "/auth/identity/session", 401)],
)
def test_auth_proxy_responses_never_replace_no_store_with_document_cache_policy(
    auth_header_server: str, method: str, path: str, status: int
) -> None:
    request = Request(
        auth_header_server + path, data=b"{}" if method == "POST" else None, method=method
    )
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        assert response.status == status
        assert response.headers["Cache-Control"] == "no-store, max-age=0"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        if method == "GET":
            assert (
                response.headers["Set-Cookie"]
                == "__Host-ec_login=fixture; Path=/; Secure; HttpOnly; SameSite=Lax"
            )
