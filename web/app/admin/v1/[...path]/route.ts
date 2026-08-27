import type { NextRequest } from "next/server";
import http from "node:http";
import https from "node:https";

const apiOrigin = process.env.EC_API_ORIGIN ?? "http://127.0.0.1:8000";
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
    const authority = new URL(`${request.nextUrl.protocol}//${rawHost}`);
    if (
      !loopbackHosts.has(authority.hostname)
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
  if (!origin) return true;
  if (origin.includes(",")) return false;
  try {
    return new URL(origin).origin === authority.origin;
  } catch {
    return false;
  }
}

type AdminMethod = "GET" | "POST" | "PATCH";

function allowedPath(path: string[], method: AdminMethod): boolean {
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
      "runs",
      "commands",
      // Server-computed rollups. These exist so the browser never pages the run ledger to
      // build a chart: at a 30-day window that loop exceeds the proxy timeout below and
      // renders zeros over a window that holds thousands of runs.
      "summary",
      "stages",
      "catalog-freshness",
      "source-health",
      "shape",
      "throughput",
      "concentration",
    ]).has(path[1]);
  }
  if (path.length === 3) {
    return (
      path[1] === "sources" && sourceKey.test(path[2])
    ) || (
      path[1] === "commands" && commandId.test(path[2])
    );
  }
  return (
    path.length === 4
    && path[1] === "sources"
    && sourceKey.test(path[2])
    && path[3] === "events"
  );
}

function requestBackend(
  target: URL,
  method: AdminMethod,
  body: string | undefined,
): Promise<Response> {
  return new Promise((resolve) => {
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
          Host: "127.0.0.1:8000",
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
            upstreamResponse.destroy(new Error("admin response exceeded its bound"));
            return;
          }
          chunks.push(chunk);
        });
        upstreamResponse.on("end", () => {
          const payload = Buffer.concat(chunks);
          resolve(new Response(payload, {
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
          resolve(jsonError("ingestion administration is unavailable", 503));
        });
      },
    );
    upstream.setTimeout(upstreamTimeoutMs, () => {
      upstream.destroy(new Error("admin upstream timed out"));
    });
    upstream.on("error", () => {
      resolve(jsonError("ingestion administration is unavailable", 503));
    });
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
  if (!authority) return jsonError("local ingestion admin requires a loopback Host", 403);
  if (!sameOrigin(request, authority)) {
    return jsonError("cross-origin admin command rejected", 403);
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
    body = await request.text();
    if (new TextEncoder().encode(body).byteLength > maxCommandBytes) {
      return jsonError("admin command is too large", 413);
    }
  }

  const target = new URL(`/admin/v1/${path.map(encodeURIComponent).join("/")}`, apiOrigin);
  target.search = request.nextUrl.search;

  try {
    return await requestBackend(target, method, body);
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
