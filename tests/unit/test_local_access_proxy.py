"""Exercise the local Caddy front door against disposable, non-provider upstreams."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Upstream:
    server: ThreadingHTTPServer
    records: list[dict[str, object]]
    thread: threading.Thread
    stopped: bool = False

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def stop(self) -> None:
        if not self.stopped:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
            self.stopped = True


@contextmanager
def upstream(name: str) -> Iterator[Upstream]:
    records: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.respond()

        def do_POST(self) -> None:
            self.respond()

        def respond(self) -> None:
            record = {
                "upstream": name,
                "method": self.command,
                "path": self.path,
                "host": self.headers.get("Host"),
                "origin": self.headers.get("Origin"),
                "cookie": self.headers.get("Cookie"),
                "body": self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode(),
            }
            records.append(record)
            # A harmless fixture models the admin application's exact-origin mutation gate.
            origin = self.headers.get("Origin")
            allowed = (
                name != "admin"
                or self.command == "GET"
                or (origin == f"http://{self.headers.get('Host')}")
            )
            payload = json.dumps(record).encode()
            self.send_response(200 if allowed else 403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    instance = Upstream(server, records, thread)
    thread.start()
    try:
        yield instance
    finally:
        instance.stop()


@dataclass
class Proxy:
    port: int
    admin: Upstream
    consumer: Upstream
    configuration: dict[str, object]

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: str | None = None,
    ) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()


@pytest.fixture
def proxy(tmp_path: Path) -> Iterator[Proxy]:
    requested = os.environ.get("EC_CADDY_BINARY", "caddy")
    binary = shutil.which(requested)
    if not binary and requested == "caddy" and Path("/opt/homebrew/bin/caddy").is_file():
        binary = "/opt/homebrew/bin/caddy"
    if not binary:
        pytest.skip("Caddy is required; set EC_CADDY_BINARY or install it on PATH")

    with ExitStack() as stack:
        consumer = stack.enter_context(upstream("consumer"))
        admin = stack.enter_context(upstream("admin"))
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = int(reservation.getsockname()[1])
        source = (ROOT / "deploy/local-access.Caddyfile").read_text()
        for expected, actual in ((14001, port), (14002, admin.port), (14011, consumer.port)):
            assert str(expected) in source
            source = source.replace(str(expected), str(actual))
        config = tmp_path / "Caddyfile"
        config.write_text(source)
        adapted = subprocess.run(
            [binary, "adapt", "--config", str(config), "--adapter", "caddyfile"],
            check=True,
            capture_output=True,
            text=True,
        )
        configuration = json.loads(adapted.stdout)
        log = tmp_path / "caddy.log"
        output = stack.enter_context(log.open("w"))
        process = subprocess.Popen(
            [binary, "run", "--config", str(config), "--adapter", "caddyfile"],
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(f"Caddy exited during startup: {log.read_text()}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.02)
            else:
                pytest.fail(f"Caddy did not listen: {log.read_text()}")
            yield Proxy(port, admin, consumer, configuration)
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


@pytest.mark.parametrize(
    ("path", "destination"),
    [
        ("/", "consumer"),
        ("/auth/callback?state=fixture", "consumer"),
        ("/v1/catalog/search?q=music", "consumer"),
        ("/_next/static/fixture.js", "consumer"),
        ("/administer", "consumer"),
        ("/admin", "admin"),
        ("/admin/", "admin"),
        ("/admin/review", "admin"),
        ("/admin/v1/ingestion/overview?window_hours=24", "admin"),
    ],
)
def test_paths_keep_their_prefix_and_query(proxy: Proxy, path: str, destination: str) -> None:
    status, body = proxy.request(path)
    assert status == 200
    received = json.loads(body)
    assert received["upstream"] == destination
    assert received["path"] == path
    other = proxy.consumer if destination == "admin" else proxy.admin
    assert other.records == []


@pytest.mark.parametrize(
    ("path", "destination"),
    [
        ("/admin/v1/fixture?next=%2Fsettings&q=a%20b", "admin"),
        ("/auth/fixture?next=%2Fsettings&q=a%20b", "consumer"),
    ],
)
def test_requests_preserve_same_origin_host_cookie_and_body(
    proxy: Proxy, path: str, destination: str
) -> None:
    body = '{"fixture":true}'
    status, payload = proxy.request(
        path,
        method="POST",
        headers={
            "Origin": proxy.origin,
            "Cookie": "fixture=synthetic",
            "Content-Type": "application/json",
        },
        body=body,
    )
    assert status == 200
    received = json.loads(payload)
    assert received["upstream"] == destination
    assert received["method"] == "POST"
    assert received["path"] == path
    assert received["host"] == f"127.0.0.1:{proxy.port}"
    assert received["origin"] == proxy.origin
    assert received["cookie"] == "fixture=synthetic"
    assert received["body"] == body
    other = proxy.consumer if destination == "admin" else proxy.admin
    assert other.records == []


def test_cross_origin_admin_write_remains_rejected_by_application(proxy: Proxy) -> None:
    status, payload = proxy.request(
        "/admin/v1/fixture",
        method="POST",
        headers={"Origin": "https://attacker.example", "Content-Type": "application/json"},
        body="{}",
    )
    assert status == 403
    assert json.loads(payload)["origin"] == "https://attacker.example"
    assert proxy.consumer.records == []


@pytest.mark.parametrize("path", ["/", "/admin/v1/ingestion/overview"])
def test_hostile_host_never_reaches_either_upstream(proxy: Proxy, path: str) -> None:
    status, _ = proxy.request(path, headers={"Host": "attacker.example"})
    assert 400 <= status < 500
    assert proxy.consumer.records == []
    assert proxy.admin.records == []


def test_failed_consumer_never_falls_back_to_admin(proxy: Proxy) -> None:
    proxy.consumer.stop()
    status, _ = proxy.request("/v1/catalog/search")
    assert status in {502, 503, 504}
    assert proxy.admin.records == []


def test_only_loopback_listener_and_no_caddy_admin_api(proxy: Proxy) -> None:
    assert proxy.configuration["admin"]["disabled"] is True
    servers = proxy.configuration["apps"]["http"]["servers"].values()
    assert list(servers)
    for server in servers:
        assert server["listen"] == [f"127.0.0.1:{proxy.port}"]
        assert server["automatic_https"]["disable"] is True
