import type { NextRequest } from "next/server";
import http from "node:http";
import https from "node:https";

const hostedOperator = process.env.EC_OPERATOR_API_ENABLED === "true";
const apiOrigin = hostedOperator
  ? process.env.EC_OPERATOR_API_ORIGIN
  : process.env.EC_API_ORIGIN ?? "http://127.0.0.1:8000";
const operatorPublicOrigin = process.env.EC_OPERATOR_PUBLIC_ORIGIN;
const loopbackHosts = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);
const sourceKey = /^[a-z0-9][a-z0-9-]{1,79}$/;
const commandId = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const maxCommandBytes = 16_384;
const maxResponseBytes = 2 * 1_024 * 1_024;
const upstreamTimeoutMs = 10_000;

export const runtime = "nodejs";

interface RouteContext {
  params: Promise<{ path: string[] }>;
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

function browserAuthority(request: NextRequest): URL | null {
  const rawHost = request.headers.get("host");
  if (
    !rawHost
    || rawHost.includes(",")
    || [...rawHost].some((character) => {
      const code = character.codePointAt(0) ?? 0;
      return code < 0x21 || code === 0x7f;
    })
  ) {
    return null;
  }
  try {
    const configuredOrigin = hostedOperator ? new URL(operatorPublicOrigin ?? "") : null;
    if (configuredOrigin && (
      configuredOrigin.protocol !== "https:" || configuredOrigin.pathname !== "/"
      || configuredOrigin.username || configuredOrigin.password
      || configuredOrigin.search || configuredOrigin.hash
    )) return null;
    const authority = new URL(`${configuredOrigin?.protocol ?? request.nextUrl.protocol}//${rawHost}`);
    if (
      (hostedOperator ? authority.origin !== configuredOrigin?.origin : !loopbackHosts.has(authority.hostname))
      || authority.username
      || authority.password
      || authority.pathname !== "/"
    ) {
      return null;
    }
    return authority;
  } catch {
    return null;
  }
}

function sameOrigin(request: NextRequest, authority: URL): boolean {
  const origin = request.headers.get("origin");
  if (!origin) return !hostedOperator || request.method === "GET";
  if (origin.includes(",")) return false;
  try {
    const parsed = new URL(origin);
    return parsed.origin === authority.origin && parsed.pathname === "/"
      && !parsed.username && !parsed.password && !parsed.search && !parsed.hash;
  } catch {
    return false;
  }
}

type AdminMethod = "GET" | "POST" | "PATCH";

function allowedPath(path: string[], method: AdminMethod): boolean {
  if (path[0] === "models" && path.length === 2) {
    return (method === "GET" && ["usage", "budget", "key"].includes(path[1]))
      || (method === "PATCH" && path[1] === "budget");
  }
  if (method === "GET" && path.length === 2 && (
    (path[0] === "operations" && (path[1] === "overview" || path[1] === "errors" || path[1] === "records"))
    || (path[0] === "operator" && path[1] === "session")
  )) return true;
  if (path[0] !== "ingestion") return false;
  if (method === "POST") {
    return path.length === 2 && path[1] === "commands";
  }
  if (method === "PATCH") {
    return (
      path.length === 3
      && path[1] === "sources"
      && sourceKey.test(path[2])
    ) || (
      path.length === 4
      && path[1] === "sources"
      && path[2] === "bulk"
      && path[3] === "enabled"
    );
  }
  if (path.length === 2) {
    return new Set([
      "overview",
      "filters",
      "sources",
      "events",
      "runs",
      "commands",
      // Server-computed rollups. These exist so the browser never pages the run ledger to
      // build a chart: at a 30-day window that loop exceeds the proxy timeout below and
      // renders zeros over a window that holds thousands of runs.
      "summary",
      "stages",
      "catalog-freshness",
      "source-health",
      "source-registration-history",
      "shape",
      "throughput",
      "concentration",
    ]).has(path[1]);
  }
  if (path.length === 3) {
    return (
      path[1] === "runs" && path[2] === "lookup"
    ) || (
      path[1] === "sources" && sourceKey.test(path[2])
    ) || (
      path[1] === "commands" && commandId.test(path[2])
    );
  }
  return (
    path.length === 4
    && ((path[1] === "sources" && sourceKey.test(path[2]) && path[3] === "events")
      || (path[1] === "commands" && commandId.test(path[2]) && path[3] === "investigation"))
  );
}

function requestBackend(
  target: URL,
  method: AdminMethod,
  body: string | undefined,
  verifiedHeaders: Record<string, string>,
  signal?: AbortSignal,
): Promise<Response> {
  return new Promise((resolve) => {
    // Cancelling a read may release its upstream connection. A submitted write still needs
    // its receipt, even when the browser leaves the page before the response arrives.
    const readSignal = method === "GET" ? signal : undefined;
    if (readSignal?.aborted) {
      resolve(jsonError("ingestion administration read was cancelled", 499));
      return;
    }
    let settled = false;
    let deadline: ReturnType<typeof setTimeout> | undefined;
    const finish = (response: Response) => {
      if (settled) return;
      settled = true;
      if (deadline) clearTimeout(deadline);
      readSignal?.removeEventListener("abort", cancelRead);
      resolve(response);
    };
    const cancelRead = () => {
      finish(jsonError("ingestion administration read was cancelled", 499));
      upstream.destroy();
    };
    const transport = target.protocol === "https:" ? https : http;
    const upstream = transport.request(
      {
        protocol: target.protocol,
        hostname: target.hostname,
        port: target.port || (target.protocol === "https:" ? 443 : 80),
        path: `${target.pathname}${target.search}`,
        method,
        headers: {
          Accept: "application/json",
          Host: hostedOperator ? target.host : "127.0.0.1:8000",
          ...verifiedHeaders,
          ...(method !== "GET"
            ? {
                "Content-Type": "application/json",
                "Content-Length": Buffer.byteLength(body ?? ""),
              }
            : {}),
        },
      },
      (upstreamResponse) => {
        const chunks: Buffer[] = [];
        let size = 0;
        upstreamResponse.on("data", (chunk: Buffer) => {
          size += chunk.length;
          if (size > maxResponseBytes) {
            finish(jsonError("ingestion administration response exceeded its size limit", 502));
            upstreamResponse.destroy(new Error("admin response exceeded its bound"));
            return;
          }
          chunks.push(chunk);
        });
        upstreamResponse.on("end", () => {
          const payload = Buffer.concat(chunks);
          finish(new Response(payload, {
            status: upstreamResponse.statusCode ?? 502,
            headers: {
              "Cache-Control": "no-store, max-age=0",
              "Content-Type": String(
                upstreamResponse.headers["content-type"] ?? "application/json",
              ),
            },
          }));
        });
        upstreamResponse.on("error", () => {
          finish(jsonError("ingestion administration response was interrupted", 502));
        });
      },
    );
    // A wall-clock deadline also bounds DNS/connect waits and slowly trickling responses.
    deadline = setTimeout(() => {
      finish(jsonError(method === "GET"
        ? "ingestion administration request timed out; refresh to try again"
        : "ingestion administration request timed out; check its recorded outcome before submitting again", 504));
      upstream.destroy(new Error("admin upstream timed out"));
    }, upstreamTimeoutMs);
    upstream.on("error", () => {
      finish(jsonError("ingestion administration backend connection failed", 503));
    });
    readSignal?.addEventListener("abort", cancelRead, { once: true });
    if (readSignal?.aborted) { cancelRead(); return; }
    if (body !== undefined) upstream.write(body);
    upstream.end();
  });
}

async function proxyAdminRequest(
  request: NextRequest,
  context: RouteContext,
  method: AdminMethod,
): Promise<Response> {
  const authority = browserAuthority(request);
  if (!authority) return jsonError(hostedOperator
    ? "operator request origin is unavailable"
    : "local ingestion admin requires a loopback Host", 403);
  if (!sameOrigin(request, authority)) {
    return jsonError("cross-origin admin command rejected", 403);
  }

  const verifiedHeaders: Record<string, string> = {};
  if (hostedOperator) {
    const assertion = request.headers.get("x-goog-iap-jwt-assertion");
    if (!assertion || assertion.length > 8192 || assertion.includes(",")) {
      return jsonError("verified operator identity required", 401);
    }
    verifiedHeaders["X-Goog-IAP-JWT-Assertion"] = assertion;
    if (method !== "GET") {
      const fetchSite = request.headers.get("sec-fetch-site");
      if (fetchSite !== null && fetchSite !== "same-origin") {
        return jsonError("cross-origin admin command rejected", 403);
      }
      verifiedHeaders.Origin = authority.origin;
      if (fetchSite) verifiedHeaders["Sec-Fetch-Site"] = fetchSite;
    }
  }

  const { path } = await context.params;
  if (!allowedPath(path, method)) return jsonError("admin resource not found", 404);

  let body: string | undefined;
  if (method !== "GET") {
    const contentType = request.headers.get("content-type")?.split(";", 1)[0].trim();
    if (contentType !== "application/json") {
      return jsonError("admin commands require JSON", 415);
    }
    const declaredLength = Number(request.headers.get("content-length") ?? "0");
    if (Number.isFinite(declaredLength) && declaredLength > maxCommandBytes) {
      return jsonError("admin command is too large", 413);
    }
    const reader = request.body?.getReader();
    const chunks: Uint8Array[] = [];
    let size = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const deadline = new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error("timeout")), upstreamTimeoutMs);
      });
      if (reader) {
        while (true) {
          const chunk = await Promise.race([reader.read(), deadline]);
          if (chunk.done) break;
          size += chunk.value.byteLength;
          if (size > maxCommandBytes) return jsonError("admin command is too large", 413);
          chunks.push(chunk.value);
        }
      }
      body = Buffer.concat(chunks).toString("utf-8");
    } catch {
      return jsonError("admin command body is unavailable", 408);
    } finally {
      if (timer) clearTimeout(timer);
      void reader?.cancel().catch(() => undefined);
    }
  }

  try {
    if (!apiOrigin) return jsonError("operator API is not configured", 503);
    const configuredApi = new URL(apiOrigin);
    if (!["http:", "https:"].includes(configuredApi.protocol)
      || configuredApi.username || configuredApi.password
      || configuredApi.pathname !== "/" || configuredApi.search || configuredApi.hash) {
      return jsonError("operator API is not configured", 503);
    }
    const target = new URL(`/admin/v1/${path.map(encodeURIComponent).join("/")}`, configuredApi);
    target.search = request.nextUrl.search;
    return await requestBackend(target, method, body, verifiedHeaders, request.signal);
  } catch {
    return jsonError("ingestion administration is unavailable", 503);
  }
}

export async function GET(request: NextRequest, context: RouteContext): Promise<Response> {
  return proxyAdminRequest(request, context, "GET");
}

export async function POST(request: NextRequest, context: RouteContext): Promise<Response> {
  return proxyAdminRequest(request, context, "POST");
}

export async function PATCH(request: NextRequest, context: RouteContext): Promise<Response> {
  return proxyAdminRequest(request, context, "PATCH");
}
