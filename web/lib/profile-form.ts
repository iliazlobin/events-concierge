/** Pure profile-form rules, mirrored from the server so the reader sees an inline error, not a 422. */

const MAX_DISPLAY_NAME = 64;

/**
 * Bidi overrides and control characters are rejected because the display name renders next to
 * product chrome: `RIGHT-TO-LEFT OVERRIDE` lets a name visually rewrite the copy around it.
 */
const FORBIDDEN = /[\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069]/u;

export function validateDisplayName(raw: string): string | null {
  const value = raw.trim();
  if (value === "") return null; // Clearing the name is allowed; it falls back to the email initial.
  if ([...value].length > MAX_DISPLAY_NAME) {
    return `Use ${MAX_DISPLAY_NAME} characters or fewer.`;
  }
  if (FORBIDDEN.test(value)) {
    return "Remove control or text-direction characters from your name.";
  }
  if (!/[\p{L}\p{N}]/u.test(value)) {
    return "Include at least one letter or number.";
  }
  return null;
}

/** IANA zones the runtime knows about, with a safe fallback for older engines. */
export function supportedTimeZones(): string[] {
  const withValues = Intl as typeof Intl & { supportedValuesOf?: (key: string) => string[] };
  try {
    const zones = withValues.supportedValuesOf?.("timeZone");
    if (zones && zones.length > 0) return zones;
  } catch {
    // Fall through to the resolved zone below.
  }
  const resolved = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return resolved ? [resolved] : [];
}
