/**
 * What to tell a reader when an entity graph does not arrive.
 *
 * `fetch` rejects with a bare `TypeError: Failed to fetch` for every network-level failure — the
 * server restarting, the connection dropping, the machine waking from sleep — and putting that
 * string on screen tells a reader nothing except that something in the plumbing broke. The only
 * distinction that matters to them is whether it is worth trying again.
 */
export function readableGraphError(caught: unknown): string {
  // A TypeError out of fetch means the request never reached a server at all; an HTTP failure
  // arrives as a normal Error carrying the status the API chose to report.
  if (caught instanceof TypeError) return "Could not reach the server.";
  if (caught instanceof Error && caught.message.trim()) return caught.message;
  return "This entity graph could not be loaded.";
}
