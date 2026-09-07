import type { NextRequest } from "next/server";
import { proxyRuntimeApiRequest } from "@/lib/runtime-api-proxy";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function proxy(request: NextRequest): Promise<Response> {
  return proxyRuntimeApiRequest(request, "/healthz");
}

export const DELETE = proxy;
export const GET = proxy;
export const HEAD = proxy;
export const OPTIONS = proxy;
export const PATCH = proxy;
export const POST = proxy;
export const PUT = proxy;
