import type { NextRequest } from "next/server";
import { proxyRuntimeApiRequest } from "@/lib/runtime-api-proxy";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ path?: string[] }>;
}

async function proxy(request: NextRequest, context: RouteContext): Promise<Response> {
  const { path = [] } = await context.params;
  return proxyRuntimeApiRequest(request, "/auth", path);
}

export const DELETE = proxy;
export const GET = proxy;
export const HEAD = proxy;
export const OPTIONS = proxy;
export const PATCH = proxy;
export const POST = proxy;
export const PUT = proxy;
