import type { NextRequest } from "next/server";
import { proxyRuntimeApiRequest } from "@/lib/runtime-api-proxy";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ helper: string }>;
}

async function proxy(request: NextRequest, context: RouteContext): Promise<Response> {
  const { helper } = await context.params;
  return proxyRuntimeApiRequest(request, "/__/auth", [helper]);
}

export const GET = proxy;
export const POST = proxy;
