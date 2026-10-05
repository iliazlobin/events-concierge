import assert from "node:assert/strict";
import { once } from "node:events";
import http from "node:http";
import test from "node:test";

async function route(t, environment) {
  const keys = ["EC_OPERATOR_API_ENABLED", "EC_OPERATOR_API_ORIGIN", "EC_OPERATOR_PUBLIC_ORIGIN", "EC_API_ORIGIN"];
  const previous = Object.fromEntries(keys.map((key) => [key, process.env[key]]));
  for (const key of keys) {
    if (environment[key] === undefined) delete process.env[key];
    else process.env[key] = environment[key];
  }
  t.after(() => {
    for (const key of keys) {
      if (previous[key] === undefined) delete process.env[key];
      else process.env[key] = previous[key];
    }
  });
  return import(`../app/admin/v1/[...path]/route.ts?run=${Math.random()}`);
}

function request(path, method = "GET", headers = {}, body) {
  const req = new Request(`https://ops.example.test/admin/v1/${path.join("/")}`, {
    method, headers: { Host: "ops.example.test", ...headers }, body,
  });
  req.nextUrl = new URL(req.url);
  return [req, { params: Promise.resolve({ path }) }];
}

async function backend(t, respond) {
  const received = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      received.push({ headers: req.headers, path: req.url, body: Buffer.concat(chunks).toString() });
      res.setHeader("Content-Type", "application/json");
      if (respond) respond(req, res);
      else res.end('{"ok":true}');
    });
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(async () => { server.closeAllConnections(); server.close(); await once(server, "close"); });
  return { origin: `http://127.0.0.1:${server.address().port}`, received };
}

test("hosted operator proxy forwards signed assertion and fixed origin without consumer credentials", async (t) => {
  const upstream = await backend(t);
  const api = await route(t, {
    EC_OPERATOR_API_ENABLED: "true", EC_OPERATOR_API_ORIGIN: upstream.origin,
    EC_OPERATOR_PUBLIC_ORIGIN: "https://ops.example.test", EC_API_ORIGIN: "https://consumer.example.test",
  });
  const response = await api.POST(...request(["ingestion", "commands"], "POST", {
    Origin: "https://ops.example.test", "Content-Type": "application/json",
    "X-Goog-IAP-JWT-Assertion": "signed.assertion.bytes", "Sec-Fetch-Site": "same-origin",
    Authorization: "Bearer consumer", Cookie: "consumer-session=secret",
    "X-Goog-Authenticated-User-Email": "spoof@example.test",
  }, '{"action":"refresh_due"}'));
  assert.equal(response.status, 200);
  assert.equal(upstream.received.length, 1);
  const received = upstream.received[0];
  assert.equal(received.headers["x-goog-iap-jwt-assertion"], "signed.assertion.bytes");
  assert.equal(received.headers.origin, "https://ops.example.test");
  assert.equal(received.headers.authorization, undefined);
  assert.equal(received.headers.cookie, undefined);
  assert.equal(received.headers["x-goog-authenticated-user-email"], undefined);
  assert.equal(received.path, "/admin/v1/ingestion/commands");
  assert.equal(response.headers.get("cache-control"), "no-store, max-age=0");
});

test("hosted proxy rejects unsigned and cross-origin mutations before backend contact", async (t) => {
  const upstream = await backend(t);
  const api = await route(t, {
    EC_OPERATOR_API_ENABLED: "true", EC_OPERATOR_API_ORIGIN: upstream.origin,
    EC_OPERATOR_PUBLIC_ORIGIN: "https://ops.example.test",
  });
  for (const headers of [
    { Origin: "https://ops.example.test" },
    { "X-Goog-IAP-JWT-Assertion": "signed.jwt.bytes" },
    { Origin: "https://consumer.example.test", "X-Goog-IAP-JWT-Assertion": "signed.jwt.bytes" },
    { Origin: "https://ops.example.test", "X-Goog-IAP-JWT-Assertion": "signed.jwt.bytes", "Sec-Fetch-Site": "cross-site" },
  ]) {
    const response = await api.POST(...request(["ingestion", "commands"], "POST", {
      "Content-Type": "application/json", ...headers,
    }, "{}"));
    assert.ok([401, 403].includes(response.status));
  }
  assert.equal(upstream.received.length, 0);
});

test("model usage proxy allows only named reads and an origin-checked budget edit", async (t) => {
  const upstream = await backend(t);
  const api = await route(t, { EC_OPERATOR_API_ENABLED: "true", EC_OPERATOR_API_ORIGIN: upstream.origin,
    EC_OPERATOR_PUBLIC_ORIGIN: "https://ops.example.test" });
  const headers = { "X-Goog-IAP-JWT-Assertion": "signed.jwt.bytes", Origin: "https://ops.example.test" };
  for (const path of ["usage", "budget", "key"]) {
    assert.equal((await api.GET(...request(["models", path], "GET", headers))).status, 200);
  }
  assert.equal((await api.PATCH(...request(["models", "budget"], "PATCH", {
    ...headers, "Content-Type": "application/json",
  }, '{"expected_revision":1,"mode":"warn"}'))).status, 200);
  assert.equal((await api.PATCH(...request(["models", "budget"], "PATCH", {
    ...headers, Origin: "https://attacker.test", "Content-Type": "application/json",
  }, "{}"))).status, 403);
  for (const path of [["models", "credits"], ["models", "keys", "new"], ["models", "usage", "raw"]]) {
    assert.equal((await api.GET(...request(path, "GET", headers))).status, 404);
  }
  assert.equal(upstream.received.length, 4);
});

test("source registration history is an aggregate-only named read path", async (t) => {
  const upstream = await backend(t);
  const api = await route(t, { EC_OPERATOR_API_ENABLED: "true", EC_OPERATOR_API_ORIGIN: upstream.origin,
    EC_OPERATOR_PUBLIC_ORIGIN: "https://ops.example.test" });
  const headers = { "X-Goog-IAP-JWT-Assertion": "signed.jwt.bytes", Origin: "https://ops.example.test" };
  const [req, ctx] = request(["ingestion", "source-registration-history"], "GET", headers);
  req.nextUrl.search = "?window_days=90&include_fixtures=false";
  assert.equal((await api.GET(req, ctx)).status, 200);
  assert.equal(upstream.received[0].path, `/admin/v1/ingestion/source-registration-history${req.nextUrl.search}`);
  assert.equal((await api.POST(...request(["ingestion", "source-registration-history"], "POST", headers, "{}"))).status, 404);
  assert.equal((await api.GET(...request(["ingestion", "source-registration-history", "raw"], "GET", headers))).status, 404);
  assert.equal(upstream.received.length, 1);
});

test("hosted proxy exposes only named operator read paths and never falls back to consumer API", async (t) => {
  const api = await route(t, {
    EC_OPERATOR_API_ENABLED: "true", EC_OPERATOR_PUBLIC_ORIGIN: "https://ops.example.test",
    EC_API_ORIGIN: "https://consumer.example.test",
  });
  const headers = { "X-Goog-IAP-JWT-Assertion": "signed.jwt.bytes" };
  for (const path of [["operator", "session"], ["operations", "overview"], ["operations", "errors"], ["operations", "records"]]) {
    assert.equal((await api.GET(...request(path, "GET", headers))).status, 503);
  }
  assert.equal((await api.GET(...request(["operations", "users"], "GET", headers))).status, 404);
});

test("work error investigation is a named read path without a retry mutation", async (t) => {
  const upstream = await backend(t);
  const api = await route(t, { EC_OPERATOR_API_ENABLED: "true", EC_OPERATOR_API_ORIGIN: upstream.origin,
    EC_OPERATOR_PUBLIC_ORIGIN: "https://ops.example.test" });
  const headers = { "X-Goog-IAP-JWT-Assertion": "signed.jwt.bytes", Origin: "https://ops.example.test" };
  const [req, ctx] = request(["operations", "errors"], "GET", headers);
  req.nextUrl.search = "?queue=request_start&offset=10&limit=10&record_id=019a7137-8b68-7bf4-b75c-000100000001";
  assert.equal((await api.GET(req, ctx)).status, 200);
  assert.equal(upstream.received[0].path, `/admin/v1/operations/errors${req.nextUrl.search}`);
  assert.equal((await api.POST(...request(["operations", "errors"], "POST", headers, "{}"))).status, 404);
  assert.equal((await api.GET(...request(["operations", "errors", "private"], "GET", headers))).status, 404);
  assert.equal(upstream.received.length, 1);
});

test("local proxy retains loopback access and never forwards operator assertions", async (t) => {
  const upstream = await backend(t);
  const api = await route(t, { EC_API_ORIGIN: upstream.origin });
  const [req, ctx] = request(["operator", "session"], "GET", {
    Host: "127.0.0.1:3001", "X-Goog-IAP-JWT-Assertion": "ignored.jwt.bytes",
  });
  req.nextUrl = new URL("http://127.0.0.1:3001/admin/v1/operator/session");
  const response = await api.GET(req, ctx);
  assert.equal(response.status, 200);
  assert.equal(upstream.received[0].headers["x-goog-iap-jwt-assertion"], undefined);
});

test("exact run lookup forwards identity only on the named read path", async (t) => {
  const upstream = await backend(t);
  const api = await route(t, { EC_OPERATOR_API_ENABLED: "true", EC_OPERATOR_API_ORIGIN: upstream.origin,
    EC_OPERATOR_PUBLIC_ORIGIN: "https://ops.example.test" });
  const headers = { "X-Goog-IAP-JWT-Assertion": "signed.jwt.bytes" };
  const [req, ctx] = request(["ingestion", "runs", "lookup"], "GET", headers);
  req.nextUrl.search = "?source_key=bay-arts&run_key=cadence%3Abay-arts%3Aexact&include_fixtures=false";
  assert.equal((await api.GET(req, ctx)).status, 200);
  assert.equal(upstream.received[0].path, `/admin/v1/ingestion/runs/lookup${req.nextUrl.search}`);
  assert.equal((await api.GET(...request(["ingestion", "runs", "private"], "GET", headers))).status, 404);
  assert.equal((await api.POST(...request(["ingestion", "runs", "lookup"], "POST", {
    ...headers, Origin: "https://ops.example.test", "Content-Type": "application/json",
  }, "{}"))).status, 404);
});

test("investigation GET forwards bounded cursor and exact source selection without exposing arbitrary command routes", async (t) => {
  const upstream = await backend(t);
  const api = await route(t, { EC_OPERATOR_API_ENABLED: "true", EC_OPERATOR_API_ORIGIN: upstream.origin,
    EC_OPERATOR_PUBLIC_ORIGIN: "https://ops.example.test" });
  const id = "926b8762-6d9f-5ade-b85a-b024ceea89cb";
  const [req, ctx] = request(["ingestion", "commands", id, "investigation"], "GET", {
    "X-Goog-IAP-JWT-Assertion": "signed.jwt.bytes",
  });
  req.nextUrl.search = "?after_event_id=9007199254740993&limit=100&source_key=bay-arts";
  assert.equal((await api.GET(req, ctx)).status, 200);
  assert.equal(upstream.received[0].path, `/admin/v1/ingestion/commands/${id}/investigation${req.nextUrl.search}`);
  assert.equal((await api.GET(...request(["ingestion", "commands", id, "logs"], "GET", {
    "X-Goog-IAP-JWT-Assertion": "signed.jwt.bytes",
  }))).status, 404);
});

test("cancelled browser reads close the upstream request", async (t) => {
  let started;
  let closed;
  const contacted = new Promise((resolve) => { started = resolve; });
  const disconnected = new Promise((resolve) => { closed = resolve; });
  const upstream = await backend(t, (_req, res) => {
    res.on("close", closed);
    started();
  });
  const api = await route(t, { EC_API_ORIGIN: upstream.origin });
  const controller = new AbortController();
  const [original, ctx] = request(["ingestion", "sources"], "GET", { Host: "127.0.0.1:3001" });
  const req = new Request(original, { signal: controller.signal });
  req.nextUrl = new URL("http://127.0.0.1:3001/admin/v1/ingestion/sources");
  const pending = api.GET(req, ctx);
  await contacted;
  controller.abort();
  assert.equal((await pending).status, 499);
  await disconnected;
  assert.equal(upstream.received.length, 1);
});

test("browser cancellation does not discard the receipt of a submitted write", async (t) => {
  let release;
  let started;
  const contacted = new Promise((resolve) => { started = resolve; });
  const upstream = await backend(t, (_req, res) => { release = () => res.end('{"accepted":true}'); started(); });
  const api = await route(t, { EC_API_ORIGIN: upstream.origin });
  const controller = new AbortController();
  const [original, ctx] = request(["ingestion", "commands"], "POST", {
    Host: "127.0.0.1:3001", "Content-Type": "application/json",
  }, '{"action":"refresh_due"}');
  const req = new Request(original, { signal: controller.signal });
  req.nextUrl = new URL("http://127.0.0.1:3001/admin/v1/ingestion/commands");
  const pending = api.POST(req, ctx);
  await contacted;
  controller.abort();
  release();
  const result = await pending;
  assert.equal(result.status, 200);
  assert.deepEqual(await result.json(), { accepted: true });
  assert.equal(upstream.received.length, 1);
});

test("a trickling backend response cannot extend the absolute request deadline", { timeout: 15000 }, async (t) => {
  const upstream = await backend(t, (_req, res) => {
    res.write(" ");
    const trickle = setInterval(() => res.write(" "), 50);
    res.on("close", () => clearInterval(trickle));
  });
  const api = await route(t, { EC_API_ORIGIN: upstream.origin });
  const [req, ctx] = request(["ingestion", "sources"], "GET", { Host: "127.0.0.1:3001" });
  req.nextUrl = new URL("http://127.0.0.1:3001/admin/v1/ingestion/sources");
  const response = await api.GET(req, ctx);
  assert.equal(response.status, 504);
  assert.match((await response.json()).detail, /request timed out/);
  assert.equal(upstream.received.length, 1);
});


test("record investigation forwards exact decimal references and stays read only", async (t) => {
  const upstream = await backend(t);
  const api = await route(t, { EC_OPERATOR_API_ENABLED: "true", EC_OPERATOR_API_ORIGIN: upstream.origin,
    EC_OPERATOR_PUBLIC_ORIGIN: "https://ops.example.test" });
  const headers = { "X-Goog-IAP-JWT-Assertion": "signed.jwt.bytes", Origin: "https://ops.example.test" };
  const [req, ctx] = request(["operations", "records"], "GET", headers);
  req.nextUrl.search = "?queue=notifications&scope=failed&record_id=-9222999999738606380";
  assert.equal((await api.GET(req, ctx)).status, 200);
  assert.equal(upstream.received[0].path, `/admin/v1/operations/records${req.nextUrl.search}`);
  assert.equal((await api.POST(...request(["operations", "records"], "POST", headers, "{}"))).status, 404);
  assert.equal((await api.GET(...request(["operations", "records", "private"], "GET", headers))).status, 404);
  assert.equal(upstream.received.length, 1);
});
