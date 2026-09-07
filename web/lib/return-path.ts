/**
 * Where a post-login or post-reauth hop is allowed to land.
 *
 * The server permits only `/` or `/app` as a return path, so the settings destination rides along in
 * the fragment. This resolves that fragment against a fixed allowlist -- never against arbitrary
 * input -- so the hop cannot become an open redirect.
 */
const ALLOWED = new Set([
  "/settings",
  "/settings/taste",
  "/settings/activity",
  "/settings/account",
]);

const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/;

export function resolveReturnPath(hash: string): string {
  if (!hash) return "/";
  const candidate = hash.startsWith("#") ? hash.slice(1) : hash;
  if (!candidate.startsWith("/") || candidate.startsWith("//")) return "/";
  if (candidate.includes("\\") || candidate.includes(":")) return "/";
  if (CONTROL_CHARACTERS.test(candidate)) return "/";
  if (candidate.length > 256) return "/";
  return ALLOWED.has(candidate) ? candidate : "/";
}
