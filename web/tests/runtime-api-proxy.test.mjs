import assert from "node:assert/strict";
import { once } from "node:events";
import http from "node:http";
import test from "node:test";

import { proxyRuntimeApiRequest } from "../lib/runtime-api-proxy.ts";

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
          Cookie: "session=abc",
          "Content-Type": "application/json",
          "X-Forwarded-Host": "attacker.example",
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
      cookie: "session=abc",
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
