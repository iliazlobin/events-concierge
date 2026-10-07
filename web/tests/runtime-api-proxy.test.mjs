import assert from "node:assert/strict";
import { once } from "node:events";
import http from "node:http";
import test from "node:test";

import { proxyRuntimeApiRequest } from "../lib/runtime-api-proxy.ts";

test("Firebase helper mode strips credentials, preserves callback bytes and CSP, and bounds output", async (t) => {
  const originalFetch = global.fetch;
  const originalOrigin = process.env.EC_API_ORIGIN;
  process.env.EC_API_ORIGIN = "http://api.internal:8000";
  t.after(() => { global.fetch = originalFetch; process.env.EC_API_ORIGIN = originalOrigin; });
  let calls = 0;
  global.fetch = async (url, options) => {
    calls++;
    assert.equal(String(url), "http://api.internal:8000/__/auth/handler?state=fixture&code=one%2Btwo");
    assert.equal(options.redirect, "manual");
    assert.equal(options.method, "POST");
    assert.equal(new TextDecoder().decode(options.body), "code=fixture&state=one");
    assert.equal(options.headers.get("content-type"), "application/x-www-form-urlencoded");
    for (const name of ["cookie", "authorization", "x-goog-iap-jwt-assertion", "cf-access-jwt-assertion", "origin", "referer", "x-forwarded-host", "x-middleware-subrequest"]) {
      assert.equal(options.headers.get(name), null);
    }
    return new Response("<script>fixture</script>", { headers: {
      "Content-Type": "text/html", "Content-Security-Policy": "script-src 'self'", "Set-Cookie": "__Host-ec_session=untrusted",
      "Cache-Control": "public", "X-Debug": "untrusted",
    } });
  };
  const result = await proxyRuntimeApiRequest(new Request("https://events.example/__/auth/handler?state=fixture&code=one%2Btwo", {
    method: "POST", body: "code=fixture&state=one", headers: {
      "Content-Type": "application/x-www-form-urlencoded", Cookie: "__Host-ec_session=consumer; GCP_IAP_AUTH_TOKEN=admin",
      Authorization: "Bearer consumer", "X-Goog-IAP-JWT-Assertion": "signed.jwt.bytes", "Cf-Access-Jwt-Assertion": "signed.jwt.bytes",
      Origin: "https://events.example", Referer: "https://events.example/admin?secret=fixture", "X-Middleware-Subrequest": "untrusted",
    },
  }), "/__/auth", ["handler"]);
  assert.equal(calls, 1);
  assert.equal(result.status, 200);
  assert.equal(await result.text(), "<script>fixture</script>");
  assert.equal(result.headers.get("content-security-policy"), "script-src 'self'");
  assert.equal(result.headers.get("cache-control"), "no-store, max-age=0");
  assert.equal(result.headers.get("set-cookie"), null);
  assert.equal(result.headers.get("x-debug"), null);
  global.fetch = async () => new Response(new Uint8Array(2 * 1024 * 1024 + 1));
  assert.equal((await proxyRuntimeApiRequest(new Request("https://events.example/__/auth/iframe"), "/__/auth", ["iframe"])).status, 502);
});

test("Firebase helper mode refuses unlisted, encoded and extra paths before fetch", async (t) => {
  const originalFetch = global.fetch;
  const originalOrigin = process.env.EC_API_ORIGIN;
  process.env.EC_API_ORIGIN = "http://api.internal:8000";
  t.after(() => { global.fetch = originalFetch; process.env.EC_API_ORIGIN = originalOrigin; });
  global.fetch = async () => assert.fail("unexpected upstream call");
  for (const [path, segments] of [
    ["/__/auth/%68andler", ["handler"]], ["/__/auth/handler/", ["handler"]],
    ["/__/auth/credential", ["credential"]], ["/__/auth/handler/extra", ["handler", "extra"]],
    ["/__/auth/iframe%2f", ["iframe/"]], ["/__/auth/action", ["action"]],
  ]) assert.equal((await proxyRuntimeApiRequest(new Request("https://events.example" + path), "/__/auth", segments)).status, 404);
  for (const method of ["PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"]) {
    assert.equal((await proxyRuntimeApiRequest(new Request("https://events.example/__/auth/handler", { method }), "/__/auth", ["handler"])).status, 405);
  }
  assert.equal((await proxyRuntimeApiRequest(new Request("https://events.example/__/auth/iframe", { method: "POST" }), "/__/auth", ["iframe"])).status, 405);
});

async function listen(handler) {
  const server = http.createServer(handler);
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  return {
    origin: `http://127.0.0.1:${address.port}`,
    async close() {
      server.close();
      await once(server, "close");
    },
  };
}

test("one imported proxy honors the runtime EC_API_ORIGIN and preserves requests", async (t) => {
  const originalOrigin = process.env.EC_API_ORIGIN;
  t.after(() => {
    if (originalOrigin === undefined) delete process.env.EC_API_ORIGIN;
    else process.env.EC_API_ORIGIN = originalOrigin;
  });

  const backends = await Promise.all(["blue", "green"].map((name) => listen((request, response) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      response.statusCode = 201;
      response.setHeader("content-type", "application/json");
      response.setHeader("x-backend", name);
      response.end(JSON.stringify({
        name,
        method: request.method,
        url: request.url,
        body: Buffer.concat(chunks).toString("utf8"),
        authorization: request.headers.authorization,
        cookie: request.headers.cookie,
        spoofedForwarding: request.headers["x-forwarded-host"],
        operatorIap: request.headers["x-goog-iap-jwt-assertion"],
        operatorAccess: request.headers["cf-access-jwt-assertion"],
        operatorEmail: request.headers["cf-access-authenticated-user-email"],
      }));
    });
  })));
  t.after(async () => Promise.all(backends.map((backend) => backend.close())));

  for (const backend of backends) {
    process.env.EC_API_ORIGIN = backend.origin;
    const response = await proxyRuntimeApiRequest(new Request(
      "https://events.example/v1/catalog/events?q=music",
      {
        method: "POST",
        headers: {
          Authorization: "Bearer user-token",
          Cookie: "__Host-ec_session=abc",
          "Content-Type": "application/json",
          "X-Forwarded-Host": "attacker.example",
          "X-Goog-IAP-JWT-Assertion": "signed.iap.bytes",
          "Cf-Access-Jwt-Assertion": "signed.cf.bytes",
          "Cf-Access-Authenticated-User-Email": "iliazlobin91@gmail.com",
        },
        body: '{"city":"Oakland"}',
      },
    ), "/v1", ["catalog", "events"]);

    assert.equal(response.status, 201);
    assert.equal(response.headers.get("x-backend"), backend === backends[0] ? "blue" : "green");
    assert.deepEqual(await response.json(), {
      name: backend === backends[0] ? "blue" : "green",
      method: "POST",
      url: "/v1/catalog/events?q=music",
      body: '{"city":"Oakland"}',
      authorization: "Bearer user-token",
      cookie: "__Host-ec_session=abc",
    });
  }
});

test("proxy preserves status, response headers, redirects, and separate cookies", async (t) => {
  const originalOrigin = process.env.EC_API_ORIGIN;
  t.after(() => {
    if (originalOrigin === undefined) delete process.env.EC_API_ORIGIN;
    else process.env.EC_API_ORIGIN = originalOrigin;
  });

  const backend = await listen((request, response) => {
    if (request.url === "/auth/start") {
      response.statusCode = 307;
      response.setHeader("location", `${backend.origin}/auth/callback?state=ok`);
      response.setHeader("set-cookie", [
        "oauth_state=one; HttpOnly; SameSite=Lax; Path=/",
        "csrf=two; Secure; SameSite=Strict; Path=/auth",
      ]);
      response.setHeader("x-preserved", "yes");
      response.setHeader("connection", "x-upstream-hop");
      response.setHeader("x-upstream-hop", "remove-me");
      response.end();
      return;
    }
    response.statusCode = 404;
    response.end();
  });
  t.after(() => backend.close());
  process.env.EC_API_ORIGIN = backend.origin;

  const response = await proxyRuntimeApiRequest(
    new Request("https://events.example/auth/start"),
    "/auth",
    ["start"],
  );

  assert.equal(response.status, 307);
  assert.equal(response.headers.get("location"), "/auth/callback?state=ok");
  assert.equal(response.headers.get("x-preserved"), "yes");
  assert.equal(response.headers.has("x-upstream-hop"), false);
  assert.deepEqual(response.headers.getSetCookie(), [
    "oauth_state=one; HttpOnly; SameSite=Lax; Path=/",
    "csrf=two; Secure; SameSite=Strict; Path=/auth",
  ]);
});

test("shared-origin IAP cookies stay out of consumer APIs without hiding duplicate consumer cookies", async (t) => {
  const originalOrigin = process.env.EC_API_ORIGIN;
  t.after(() => {
    if (originalOrigin === undefined) delete process.env.EC_API_ORIGIN;
    else process.env.EC_API_ORIGIN = originalOrigin;
  });
  const backend = await listen((request, response) => {
    response.setHeader("content-type", "application/json");
    response.end(JSON.stringify({ cookie: request.headers.cookie ?? null,
      authorization: request.headers.authorization ?? null,
      csrf: request.headers["x-csrf-token"] ?? null }));
  });
  t.after(() => backend.close());
  process.env.EC_API_ORIGIN = backend.origin;
  const response = await proxyRuntimeApiRequest(new Request("https://events.example/auth/session", {
    headers: { Cookie: "GCP_IAP_AUTH_TOKEN_x=admin; __Host-ec_login=login; __Host-ec_session=one; __Host-ec_csrf=csrf; __Host-ec_session=two; unrelated=discard",
      Authorization: "Bearer local-token", "X-CSRF-Token": "csrf" },
  }), "/auth", ["session"]);
  assert.deepEqual(await response.json(), {
    cookie: "__Host-ec_login=login; __Host-ec_session=one; __Host-ec_csrf=csrf; __Host-ec_session=two",
    authorization: "Bearer local-token", csrf: "csrf",
  });
  const adminOnly = await proxyRuntimeApiRequest(new Request("https://events.example/v1/events", {
    headers: { Cookie: "GCP_IAP_AUTH_TOKEN_x=admin; gcpiap_authmode=AUTHENTICATING" },
  }), "/v1", ["events"]);
  assert.equal((await adminOnly.json()).cookie, null);
});

test("proxy fails closed for invalid origins and ambiguous or overlong paths", async (t) => {
  const originalOrigin = process.env.EC_API_ORIGIN;
  const originalNodeEnv = process.env.NODE_ENV;
  t.after(() => {
    if (originalOrigin === undefined) delete process.env.EC_API_ORIGIN;
    else process.env.EC_API_ORIGIN = originalOrigin;
    if (originalNodeEnv === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = originalNodeEnv;
  });

  delete process.env.EC_API_ORIGIN;
  process.env.NODE_ENV = "production";
  const missingProductionOrigin = await proxyRuntimeApiRequest(
    new Request("https://events.example/readyz"),
    "/readyz",
  );
  assert.equal(missingProductionOrigin.status, 503);

  process.env.EC_API_ORIGIN = "file:///etc/passwd";
  const invalidOrigin = await proxyRuntimeApiRequest(
    new Request("https://events.example/healthz"),
    "/healthz",
  );
  assert.equal(invalidOrigin.status, 503);

  process.env.EC_API_ORIGIN = "http://127.0.0.1:1";
  const traversal = await proxyRuntimeApiRequest(
    new Request("https://events.example/v1/secret"),
    "/v1",
    [".."],
  );
  assert.equal(traversal.status, 404);

  const tooLong = await proxyRuntimeApiRequest(
    new Request("https://events.example/v1/secret"),
    "/v1",
    ["x".repeat(513)],
  );
  assert.equal(tooLong.status, 404);
});

test("proxy enforces the API's 64 KiB request-body ceiling", async (t) => {
  const originalOrigin = process.env.EC_API_ORIGIN;
  t.after(() => {
    if (originalOrigin === undefined) delete process.env.EC_API_ORIGIN;
    else process.env.EC_API_ORIGIN = originalOrigin;
  });
  process.env.EC_API_ORIGIN = "http://127.0.0.1:1";

  const response = await proxyRuntimeApiRequest(
    new Request("https://events.example/v1/feed", {
      method: "POST",
      body: new Uint8Array((64 * 1_024) + 1),
    }),
    "/v1",
    ["feed"],
  );

  assert.equal(response.status, 413);
  assert.deepEqual(await response.json(), { detail: "request body is too large" });
});

test("proxy gives slow request bodies a total deadline and returns 408", async (t) => {
  const originalOrigin = process.env.EC_API_ORIGIN;
  t.after(() => {
    if (originalOrigin === undefined) delete process.env.EC_API_ORIGIN;
    else process.env.EC_API_ORIGIN = originalOrigin;
  });
  process.env.EC_API_ORIGIN = "http://127.0.0.1:1";

  let cancelled = false;
  const body = new ReadableStream({
    pull() {
      return new Promise(() => undefined);
    },
    cancel() {
      cancelled = true;
    },
  });
  const response = await proxyRuntimeApiRequest(
    new Request("https://events.example/v1/feed", {
      method: "POST",
      body,
      duplex: "half",
    }),
    "/v1",
    ["feed"],
    { requestBodyTimeoutMs: 20 },
  );

  assert.equal(response.status, 408);
  assert.deepEqual(await response.json(), { detail: "request body read timed out" });
  assert.equal(cancelled, true);
});

test("proxy distinguishes malformed request streams from size and timeout failures", async (t) => {
  const originalOrigin = process.env.EC_API_ORIGIN;
  t.after(() => {
    if (originalOrigin === undefined) delete process.env.EC_API_ORIGIN;
    else process.env.EC_API_ORIGIN = originalOrigin;
  });
  process.env.EC_API_ORIGIN = "http://127.0.0.1:1";

  const response = await proxyRuntimeApiRequest(
    new Request("https://events.example/v1/feed", {
      method: "POST",
      body: new ReadableStream({
        start(controller) {
          controller.error(new Error("malformed body"));
        },
      }),
      duplex: "half",
    }),
    "/v1",
    ["feed"],
  );

  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), { detail: "request body is invalid" });
});

test("Next config and Docker build do not capture EC_API_ORIGIN", async () => {
  const [{ readFile }, { fileURLToPath }] = await Promise.all([
    import("node:fs/promises"),
    import("node:url"),
  ]);
  const root = new URL("../", import.meta.url);
  const [nextConfig, dockerfile] = await Promise.all([
    readFile(fileURLToPath(new URL("next.config.ts", root)), "utf8"),
    readFile(fileURLToPath(new URL("Dockerfile", root)), "utf8"),
  ]);
  assert.doesNotMatch(nextConfig, /EC_API_ORIGIN|rewrites\s*\(/);
  assert.doesNotMatch(dockerfile, /ARG EC_API_ORIGIN|ENV EC_API_ORIGIN/);
});
