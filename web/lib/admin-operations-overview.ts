import type { BackendQueue } from "./backend-operations.ts";
import { queueEvidence, type QueueEvidence } from "./system-operations.ts";

export interface OperationQueueDefinition {
  name: string;
  purpose: string;
  failureScope: "pending" | "history" | "inventory" | "unknown";
  inventory?: boolean;
  commands?: boolean;
}

const DEFINITIONS: Record<string, OperationQueueDefinition> = {
  ingestion_commands: { name: "Event ingestion", purpose: "Run scheduled and admin-requested source refreshes.", failureScope: "history", commands: true },
  request_start: { name: "Request starts", purpose: "Start saved user requests and record confirmed workflow acceptance.", failureScope: "pending" },
  notifications: { name: "Notifications", purpose: "Inspect pending notification work and retained failures.", failureScope: "history" },
  entity_refresh: { name: "Entity refresh", purpose: "Refresh eligible entity profiles on their own schedule. This reports inventory and source evidence, not a leased work queue.", failureScope: "inventory", inventory: true },
  watch_projection: { name: "Registration watches", purpose: "Update watch records from saved registration changes.", failureScope: "pending" },
  change_delivery: { name: "Event changes", purpose: "Send recorded event changes to existing workflows. Provider change detection is a separate step.", failureScope: "pending" },
  calendar_repair: { name: "Calendar repair", purpose: "Correct eligible calendar differences after a workflow has closed.", failureScope: "pending" },
  handoff_expiry: { name: "Handoff expiry", purpose: "Resolve eligible expired handoffs after checking workflow state.", failureScope: "pending" },
  account_erasure: { name: "Account cleanup", purpose: "Stop new account effects and track cleanup across product data and connected services.", failureScope: "pending" },
};

export function operationQueueDefinition(name: string): OperationQueueDefinition {
  return Object.hasOwn(DEFINITIONS, name) ? DEFINITIONS[name] : {
    name,
    purpose: "The backend returned this queue, but this console has no queue-specific definition for it. Inspect the reported fields without treating them as a health verdict.",
    failureScope: "unknown",
  };
}

export interface OperationQueue {
  queue: BackendQueue;
  definition: OperationQueueDefinition;
  evidence: QueueEvidence;
  needsAttention: boolean;
  attention: string | null;
  work: string;
}

/** Keep every returned queue; interpret failures only when its backend predicate is known. */
export function operationQueues(queues: readonly BackendQueue[]): OperationQueue[] {
  return queues.map(queue => {
    const definition = operationQueueDefinition(queue.queue);
    const known = definition.failureScope !== "unknown";
    const evidence = queueEvidence(queue);
    if (!known) {
      evidence.tone = "neutral";
      evidence.label = "Definition unavailable";
      evidence.summary = "Backend counts are available; their queue-specific meaning is not mapped here.";
      evidence.failureLabel = "Reported failure signals";
      evidence.failureMeaning = "The relationship between reported failures and pending work is unknown for this queue.";
      evidence.progressLabel = "Reported progress";
    }
    const needsAttention = queue.failed > 0 && (definition.failureScope === "pending" || definition.failureScope === "inventory");
    const count = queue.failed.toLocaleString("en-US");
    return {
      queue, definition, evidence, needsAttention,
      attention: !needsAttention ? null : definition.inventory
        ? `${count} profiles with failed or blocked source evidence; they may not be due now`
        : `${count} pending with recorded errors`,
      work: definition.inventory
        ? `${queue.pending.toLocaleString("en-US")} due for refresh`
        : `${queue.pending.toLocaleString("en-US")} pending${known ? "" : " reported"}`,
    };
  });
}

/** Unknown future queue names remain bookmarkable; control characters are not accepted. */
export function operationsQueueFromUrl(href: string): string | null {
  const value = new URL(href, "http://localhost").searchParams.get("ops_queue");
  return value && value.length <= 160 && value.trim() === value && !/[\u0000-\u001f\u007f]/.test(value) ? value : null;
}

export function operationsQueueUrl(href: string, queue: string | null): string {
  const url = new URL(href, "http://localhost");
  if (queue) url.searchParams.set("ops_queue", queue);
  else url.searchParams.delete("ops_queue");
  return `${url.pathname}${url.search}${url.hash}`;
}
