import { api } from "@/lib/api";

export interface BackendQueue {
  queue: string;
  pending: number;
  ready: number;
  leased: number;
  failed: number;
  oldest_pending_at: string | null;
  last_progress_at: string | null;
}

export interface BackendOverview {
  generated_at: string;
  environment: string;
  release_revision: string;
  image_digest: string | null;
  schema_revisions: string[];
  measurement_scope: "durable_queue_state";
  worker_liveness: "not_measured";
  queues: BackendQueue[];
}

export interface OperatorSession {
  subject: string;
  role: string;
  capabilities: string[];
  environment: string;
  authentication: string;
}

export async function getBackendOperations(signal: AbortSignal) {
  const [overview, session] = await Promise.all([
    api<BackendOverview>("/admin/v1/operations/overview", { cache: "no-store", signal }),
    api<OperatorSession>("/admin/v1/operator/session", { cache: "no-store", signal }),
  ]);
  return { overview, session };
}
