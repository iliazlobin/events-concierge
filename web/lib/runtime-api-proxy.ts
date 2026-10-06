const defaultApiOrigin = "http://127.0.0.1:8000";
const maxOriginBytes = 2_048;
const maxPathBytes = 4_096;
const maxQueryBytes = 8_192;
const maxPathSegments = 64;
const maxSegmentBytes = 512;
const maxRequestBytes = 64 * 1_024;
const requestBodyTimeoutMs = 10_000;
const upstreamTimeoutMs = 30_000;
// A streamed response is open for as long as the answer takes, so the single upstream deadline
// cannot apply to it: `cleanup` only runs when the BODY finishes, leaving the abort timer armed
// for the life of the stream. An agent turn is p50 ~13s and p95 ~30s, so that deadline killed
// the slow half of them mid-answer, with no terminal frame and the 504 path long out of scope.
// Streams instead get an idle watchdog (reset per chunk, comfortably above the 15s keep-alive
// ping) and an absolute ceiling.
const streamIdleTimeoutMs = 45_000;
const streamMaxDurationMs = 300_000;

const hopByHopHeaders = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "proxy-connection",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

const untrustedForwardingHeaders = new Set([
  "forwarded",
  "x-forwarded-for",
  "x-forwarded-host",
  "x-forwarded-port",
  "x-forwarded-proto",
]);

const bodylessMethods = new Set(["GET", "HEAD"]);
const supportedMethods = new Set([
  "DELETE",
  "GET",
  "HEAD",
  "OPTIONS",
  "PATCH",
  "POST",
  "PUT",
]);

export type RuntimeApiBasePath = "/v1" | "/auth" | "/healthz" | "/readyz";

// Identity Platform, hosted OIDC and local session adapters use these fixed names.
// IAP cookies share the browser origin but must never enter the consumer API.
const consumerCookies = new Set(["__Host-ec_login", "__Host-ec_session", "__Host-ec_csrf"]);

function consumerCookieHeader(value: string): string {
  return value.split(";").map((cookie) => cookie.trim()).filter((cookie) => {
    const equals = cookie.indexOf("=");
    return equals > 0 && consumerCookies.has(cookie.slice(0, equals));
  }).join("; ");
}

export interface RuntimeApiProxyOptions {
  requestBodyTimeoutMs?: number;
}

class RequestBodyTooLargeError extends Error {}

class RequestBodyTimeoutError extends Error {}

class InvalidRequestBodyError extends Error {}

function byteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function jsonError(detail: string, status: number): Response {
  return Response.json(
    { detail },
    {
      status,
      headers: { "Cache-Control": "no-store, max-age=0" },
    },
  );
}

function apiOrigin(): URL | null {
  const configuredOrigin = process.env.EC_API_ORIGIN?.trim();
  if (!configuredOrigin && process.env.NODE_ENV === "production") return null;
  const raw = configuredOrigin || defaultApiOrigin;
  if (byteLength(raw) > maxOriginBytes) return null;

  try {
    const origin = new URL(raw);
    if (
      !new Set(["http:", "https:"]).has(origin.protocol)
      || !origin.hostname
      || origin.username
      || origin.password
      || origin.pathname !== "/"
      || origin.search
      || origin.hash
    ) {
      return null;
    }
    return origin;
  } catch {
    return null;
  }
}

function connectionHeaderNames(headers: Headers): Set<string> {
  const names = new Set<string>();
  for (const value of headers.get("connection")?.split(",") ?? []) {
    const name = value.trim().toLowerCase();
    if (/^[!#$%&'*+.^_`|~0-9a-z-]+$/.test(name)) names.add(name);
  }
  return names;
}

function upstreamRequestHeaders(request: Request, bodyLength: number | null): Headers {
  const headers = new Headers();
  const connectionHeaders = connectionHeaderNames(request.headers);
  request.headers.forEach((value, name) => {
    const normalized = name.toLowerCase();
    if (
      hopByHopHeaders.has(normalized)
      || connectionHeaders.has(normalized)
      || untrustedForwardingHeaders.has(normalized)
      || normalized === "host"
      || normalized === "content-length"
      || normalized === "accept-encoding"
      || normalized.startsWith("x-goog-")
      || normalized.startsWith("cf-access-")
      || normalized === "x-real-ip"
      || normalized.startsWith("x-middleware-")
    ) {
      return;
    }
    if (normalized === "cookie") {
      const filtered = consumerCookieHeader(value);
      if (filtered) headers.append(name, filtered);
      return;
    }
    headers.append(name, value);
  });

  // Avoid transparent decompression producing a body that no longer matches
  // the upstream Content-Encoding and Content-Length headers.
  headers.set("Accept-Encoding", "identity");
  if (bodyLength !== null) headers.set("Content-Length", String(bodyLength));
  return headers;
}

function sameOriginLocation(value: string, origin: URL): string {
  try {
    const location = new URL(value, origin);
    if (location.origin !== origin.origin) return value;
    return `${location.pathname}${location.search}${location.hash}`;
  } catch {
    return value;
  }
}

function downstreamResponseHeaders(upstream: Response, origin: URL): Headers {
  const headers = new Headers();
  const connectionHeaders = connectionHeaderNames(upstream.headers);
  const setCookies = upstream.headers.getSetCookie();
  upstream.headers.forEach((value, name) => {
    const normalized = name.toLowerCase();
    if (
      hopByHopHeaders.has(normalized)
      || connectionHeaders.has(normalized)
      || normalized === "set-cookie"
    ) {
      return;
    }
    if (normalized === "location") {
      headers.append(name, sameOriginLocation(value, origin));
      return;
    }
    if (normalized === "content-encoding" && value.toLowerCase() !== "identity") {
      return;
    }
    headers.append(name, value);
  });
  if (
    upstream.headers.has("content-encoding")
    && upstream.headers.get("content-encoding")?.toLowerCase() !== "identity"
  ) {
    headers.delete("content-length");
  }
  for (const cookie of setCookies) headers.append("Set-Cookie", cookie);
  return headers;
}

async function boundedRequestBody(
  request: Request,
  timeoutMs: number,
): Promise<ArrayBuffer | null> {
  if (bodylessMethods.has(request.method.toUpperCase())) return null;

  const declaredLength = request.headers.get("content-length");
  if (declaredLength !== null) {
    if (!/^\d+$/.test(declaredLength)) throw new InvalidRequestBodyError();
    if (Number(declaredLength) > maxRequestBytes) throw new RequestBodyTooLargeError();
  }
  if (!request.body) return new ArrayBuffer(0);

  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  let timedOut = false;
  let timeout: ReturnType<typeof setTimeout>;
  const deadline = new Promise<{ kind: "timeout" }>((resolve) => {
    timeout = setTimeout(() => resolve({ kind: "timeout" }), timeoutMs);
  });
  try {
    while (true) {
      const read = reader.read().then(
        (result) => ({ kind: "read" as const, result }),
        (error: unknown) => ({ kind: "error" as const, error }),
      );
      const outcome = await Promise.race([read, deadline]);
      if (outcome.kind === "timeout") {
        timedOut = true;
        void reader.cancel("request body read timed out").catch(() => undefined);
        throw new RequestBodyTimeoutError();
      }
      if (outcome.kind === "error") throw new InvalidRequestBodyError();

      const { done, value } = outcome.result;
      if (done) break;
      total += value.byteLength;
      if (total > maxRequestBytes) {
        try {
          await reader.cancel("request body exceeds its bound");
        } catch {
          // The size violation is authoritative even if source cancellation fails.
        }
        throw new RequestBodyTooLargeError();
      }
      chunks.push(value);
    }
  } finally {
    clearTimeout(timeout!);
    // A pending read still owns the lock when the deadline wins. Cancellation
    // settles it asynchronously; the request and its reader can then be collected.
    if (!timedOut) reader.releaseLock();
  }

  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body.buffer;
}

function targetUrl(
  origin: URL,
  request: Request,
  basePath: RuntimeApiBasePath,
  path: readonly string[],
): URL | null {
  const isLeafRoute = basePath === "/healthz" || basePath === "/readyz";
  if ((isLeafRoute && path.length) || path.length > maxPathSegments) return null;

  const encodedSegments: string[] = [];
  for (const segment of path) {
    if (
      !segment
      || segment === "."
      || segment === ".."
      || segment.includes("/")
      || segment.includes("\\")
      || byteLength(segment) > maxSegmentBytes
      || [...segment].some((character) => {
        const code = character.codePointAt(0) ?? 0;
        return code === 0 || code < 0x20 || code === 0x7f;
      })
    ) {
      return null;
    }
    encodedSegments.push(encodeURIComponent(segment));
  }

  const pathname = `${basePath}${encodedSegments.length ? `/${encodedSegments.join("/")}` : ""}`;
  const source = new URL(request.url);
  if (byteLength(pathname) > maxPathBytes || byteLength(source.search) > maxQueryBytes) {
    return null;
  }

  const target = new URL(origin);
  target.pathname = pathname;
  target.search = source.search;
  return target;
}

function responseBodyWithCleanup(
  body: ReadableStream<Uint8Array> | null,
  cleanup: () => void,
): ReadableStream<Uint8Array> | null {
  if (!body) {
    cleanup();
    return null;
  }
  const reader = body.getReader();
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        const { done, value } = await reader.read();
        if (done) {
          cleanup();
          controller.close();
        } else {
          controller.enqueue(value);
        }
      } catch (error) {
        cleanup();
        controller.error(error);
      }
    },
    async cancel(reason) {
      cleanup();
      await reader.cancel(reason);
    },
  });
}

/**
 * Relay a Server-Sent Events body with liveness watchdogs.
 *
 * On a trip it synthesizes a terminal `error` frame before closing, because a torn stream and a
 * finished one look identical to a client that only reads frames -- the browser would render a
 * half-answer as though it were whole.
 */
function streamedBody(
  body: ReadableStream<Uint8Array> | null,
  controller: AbortController,
  cleanup: () => void,
): ReadableStream<Uint8Array> | null {
  if (!body) {
    cleanup();
    return body;
  }
  const reader = body.getReader();
  const encoder = new TextEncoder();
  const startedAt = Date.now();
  let idle: ReturnType<typeof setTimeout> | undefined;
  let tripped = "";

  return new ReadableStream<Uint8Array>({
    async pull(downstream) {
      const armIdle = () => {
        clearTimeout(idle);
        idle = setTimeout(() => {
          tripped = "stream_idle_timeout";
          controller.abort(new Error(tripped));
        }, streamIdleTimeoutMs);
      };
      try {
        if (Date.now() - startedAt > streamMaxDurationMs) {
          tripped = "stream_max_duration";
          controller.abort(new Error(tripped));
        }
        armIdle();
        const { done, value } = await reader.read();
        clearTimeout(idle);
        if (done) {
          cleanup();
          downstream.close();
          return;
        }
        downstream.enqueue(value);
      } catch {
        clearTimeout(idle);
        cleanup();
        downstream.enqueue(
          encoder.encode(
            `event: error\ndata: ${JSON.stringify({
              code: tripped || "stream_interrupted",
              message: "the connection to the concierge was interrupted",
            })}\n\n`,
          ),
        );
        downstream.close();
      }
    },
    cancel(reason) {
      clearTimeout(idle);
      cleanup();
      void reader.cancel(reason);
    },
  });
}

export async function proxyRuntimeApiRequest(
  request: Request,
  basePath: RuntimeApiBasePath,
  path: readonly string[] = [],
  options: RuntimeApiProxyOptions = {},
): Promise<Response> {
  const method = request.method.toUpperCase();
  if (!supportedMethods.has(method)) return jsonError("method not allowed", 405);

  const origin = apiOrigin();
  if (!origin) return jsonError("API upstream is not configured", 503);
  const target = targetUrl(origin, request, basePath, path);
  if (!target) return jsonError("API resource not found", 404);

  let body: ArrayBuffer | null;
  try {
    const requestedBodyTimeout = options.requestBodyTimeoutMs ?? requestBodyTimeoutMs;
    const bodyTimeout = Number.isFinite(requestedBodyTimeout) && requestedBodyTimeout > 0
      ? Math.min(requestedBodyTimeout, requestBodyTimeoutMs)
      : requestBodyTimeoutMs;
    body = await boundedRequestBody(request, bodyTimeout);
  } catch (error) {
    if (error instanceof RequestBodyTooLargeError) {
      return jsonError("request body is too large", 413);
    }
    if (error instanceof RequestBodyTimeoutError) {
      return jsonError("request body read timed out", 408);
    }
    return jsonError("request body is invalid", 400);
  }

  const controller = new AbortController();
  let timedOut = false;
  const timeout = setTimeout(() => {
    timedOut = true;
    controller.abort(new Error("API upstream timed out"));
  }, upstreamTimeoutMs);
  const abort = () => controller.abort(request.signal.reason);
  if (request.signal.aborted) abort();
  else request.signal.addEventListener("abort", abort, { once: true });
  const cleanup = () => {
    clearTimeout(timeout);
    request.signal.removeEventListener("abort", abort);
  };

  try {
    const upstream = await fetch(target, {
      method,
      headers: upstreamRequestHeaders(request, body?.byteLength ?? null),
      body,
      redirect: "manual",
      signal: controller.signal,
    });
    const isStream = (upstream.headers.get("content-type") ?? "").includes("text/event-stream");
    if (isStream) {
      // Headers arrived, so the request itself did not time out. Swap the one-shot deadline for
      // liveness watchdogs before handing the body downstream.
      clearTimeout(timeout);
      const headers = downstreamResponseHeaders(upstream, origin);
      headers.set("Cache-Control", "no-store, no-transform");
      headers.set("X-Accel-Buffering", "no");
      headers.delete("Content-Length");
      return new Response(streamedBody(upstream.body, controller, cleanup), {
        status: upstream.status,
        statusText: upstream.statusText,
        headers,
      });
    }
    return new Response(responseBodyWithCleanup(upstream.body, cleanup), {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: downstreamResponseHeaders(upstream, origin),
    });
  } catch {
    cleanup();
    if (timedOut) return jsonError("API upstream timed out", 504);
    return jsonError("API upstream is unavailable", 502);
  }
}
