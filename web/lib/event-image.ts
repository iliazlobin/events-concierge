import type { EventItem } from "./types.ts";

export function eventImageUrl(
  event: Pick<EventItem, "image_url">,
): string | null {
  const value = event.image_url?.trim();
  if (!value) return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" || parsed.protocol === "http:"
      ? parsed.href
      : null;
  } catch {
    return null;
  }
}
