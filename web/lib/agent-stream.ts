/**
 * Client for `POST /v1/chat`.
 *
 * A hand-rolled SSE reader over `fetch` rather than `EventSource`, which is GET-only and cannot
 * set the tenant header. `fetch` also keeps real HTTP status codes on the initial response and
 * gives us an `AbortController` for stop-generation.
 */

export interface AgentEventCard {
  ref?: string;
  title?: string;
  name?: string;
  when?: string;
  day?: string;
  where?: string | null;
  price?: string;
  topics?: string[];
  by?: string;
  organizer?: string;
  hosts?: string[];
  speakers?: string[];
  partners?: string[];
  already_going?: number;
  signup?: string;
  signup_url?: string;
  source?: string;
  sources?: string[];
  unknown?: string[];
  kind?: string;
  roles?: string[];
  events_in_catalog?: number;
  summary?: string;
  upcoming_events?: { title: string; when: string; where: string | null }[];
  [key: string]: unknown;
}

export type AgentBlock =
  | { kind: "prose"; text: string }
  | { kind: "events"; items: AgentEventCard[]; label?: string }
  | { kind: "entities"; items: AgentEventCard[]; label?: string }
  | {
      kind: "people";
      label?: string;
      groups: {
        canonical_event_id: string;
        event_title: string;
        ref?: string;
        members: { name: string; role: string; profile_url?: string | null }[];
      }[];
    }
  | { kind: "tally"; rows: { label: string; value: number }[]; label?: string; total?: number }
  | { kind: "notice"; text: string; tone?: string; known?: number; total?: number };

/**
 * One thing pulled into the conversation's context.
 *
 * Events and organizers travel as refs, which the server re-checks against what it actually
 * minted. A quote travels as text, because there is no server-side handle for a sentence the
 * user highlighted — it is treated as words the user chose to repeat, which is what it is.
 */
const RECONNECT_DELAY_MS = 1_200;

export interface SelectionEntry {
  id: string;
  kind: "event" | "entity" | "text";
  label: string;
  ref?: string;
  text?: string;
}

export interface AgentToolStep {
  tool: string;
  args?: Record<string, unknown>;
  status?: string;
  summary?: string;
}

export interface AgentStreamHandlers {
  onToolStart?: (step: AgentToolStep) => void;
  onToolEnd?: (step: AgentToolStep) => void;
  onBlock?: (block: AgentBlock, index: number) => void;
  onEnd?: (payload: { text: string; tool_calls: number; model_calls: number }) => void;
  onError?: (payload: { code: string; message: string }) => void;
}

/**
 * Read one turn.
 *
 * Resolves only once the stream closes. If it closes without a terminal frame the caller is told
 * explicitly — a torn stream and a finished one are otherwise indistinguishable to a frame
 * reader, and a half-answer would render as though it were whole.
 */
export async function streamChatTurn(
  text: string,
  {
    tenantId,
    conversationId = "",
    selectedRefs = [],
    selectedEntityRefs = [],
    selectedQuotes = [],
    signal,
    ...handlers
  }: AgentStreamHandlers & {
    tenantId: string | null;
    conversationId?: string;
    selectedRefs?: string[];
    selectedEntityRefs?: string[];
    selectedQuotes?: string[];
    signal?: AbortSignal;
  },
): Promise<void> {
  const headers = new Headers({ "Content-Type": "application/json", Accept: "text/event-stream" });
  if (tenantId) headers.set("X-EC-Tenant-ID", tenantId);
  const body = JSON.stringify({
    text,
    conversation_id: conversationId,
    selected_refs: selectedRefs,
    selected_entity_refs: selectedEntityRefs,
    selected_quotes: selectedQuotes,
  });

  /**
   * Open the stream, retrying once on a connection-level failure.
   *
   * A `fetch` that rejects before any response has arrived means the connection never landed --
   * the API restarting, a container swap, a dropped link. Nothing ran, so re-sending is safe, and
   * riding through a two-second gap is far better than showing "Failed to fetch" for a question
   * that would have worked a moment later. Only the pre-response failure is retried: once bytes
   * are flowing, a break is handled as a truncated stream instead.
   */
  let response: Response;
  try {
    response = await fetch("/v1/chat", {
      method: "POST",
      headers,
      body,
      credentials: "same-origin",
      signal,
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    await new Promise((resolve) => setTimeout(resolve, RECONNECT_DELAY_MS));
    try {
      response = await fetch("/v1/chat", {
        method: "POST",
        headers,
        body,
        credentials: "same-origin",
        signal,
      });
    } catch {
      handlers.onError?.({
        code: "unreachable",
        message: "Could not reach the concierge. It may be restarting — try again in a moment.",
      });
      return;
    }
  }

  if (!response.ok || !response.body) {
    let message = `The concierge is unavailable (${response.status}).`;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) message = payload.detail;
    } catch {
      // Keep the bounded status message when the edge did not return JSON.
    }
    handlers.onError?.({ code: `http_${response.status}`, message });
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminated = false;

  const dispatch = (rawEvent: string, rawData: string) => {
    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(rawData) as Record<string, unknown>;
    } catch {
      return;
    }
    switch (rawEvent) {
      case "tool_start":
        handlers.onToolStart?.(payload as unknown as AgentToolStep);
        break;
      case "tool_end":
        handlers.onToolEnd?.(payload as unknown as AgentToolStep);
        break;
      case "block":
        handlers.onBlock?.(payload.block as AgentBlock, Number(payload.index ?? 0));
        break;
      case "end":
        terminated = true;
        handlers.onEnd?.(payload as unknown as { text: string; tool_calls: number; model_calls: number });
        break;
      case "error":
        terminated = true;
        handlers.onError?.(payload as unknown as { code: string; message: string });
        break;
      default:
        break;
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      let name = "message";
      const data: string[] = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith(":")) continue; // keep-alive comment
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trim());
      }
      if (data.length) dispatch(name, data.join("\n"));
      boundary = buffer.indexOf("\n\n");
    }
  }

  if (!terminated) {
    handlers.onError?.({
      code: "stream_truncated",
      message: "The answer was cut off before it finished.",
    });
  }
}
