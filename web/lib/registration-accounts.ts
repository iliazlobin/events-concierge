import { api } from "./api.ts";
import type { RegistrationProviderId } from "./registration-providers.ts";

export const REGISTRATION_SITES = [
  { id: "luma", label: "Luma" }, { id: "meetup", label: "Meetup" }, { id: "partiful", label: "Partiful" },
] as const;
export type RegistrationSite = typeof REGISTRATION_SITES[number]["id"];
export interface RegistrationAccountValues {
  expected_email: string | null;
  provider_overrides: Partial<Record<RegistrationSite, string>>;
}
export interface RegistrationAccount extends RegistrationAccountValues {
  executor: RegistrationProviderId;
  revision: number;
  updated_at: string | null;
}
export interface RegistrationAccountSnapshot {
  executor: RegistrationProviderId;
  provider: RegistrationSite;
  expected_email: string | null;
}
export type RegistrationAccountDraft = { defaultEmail: string } & Record<RegistrationSite, string>;
export const EMPTY_REGISTRATION_ACCOUNT: RegistrationAccountDraft = { defaultEmail: "", luma: "", meetup: "", partiful: "" };

/** Saved registration identities are independent of the account that owns the queue. */
export function registrationAccountDraft(value: RegistrationAccountValues): RegistrationAccountDraft {
  return { defaultEmail: value.expected_email ?? "", luma: value.provider_overrides.luma ?? "", meetup: value.provider_overrides.meetup ?? "", partiful: value.provider_overrides.partiful ?? "" };
}
export function registrationAccountValues(draft: RegistrationAccountDraft): RegistrationAccountValues {
  const provider_overrides: RegistrationAccountValues["provider_overrides"] = {};
  for (const site of REGISTRATION_SITES) {
    const value = normalizeRegistrationEmail(draft[site.id]);
    if (value) provider_overrides[site.id] = value;
  }
  return { expected_email: normalizeRegistrationEmail(draft.defaultEmail), provider_overrides };
}
export function normalizeRegistrationEmail(raw: string): string | null {
  const value = raw.replace(/^ +| +$/g, "");
  if (!value) return null;
  const separator = value.lastIndexOf("@");
  return separator < 0 ? value : value.slice(0, separator + 1) + value.slice(separator + 1).toLowerCase();
}

export function getRegistrationAccount(executor: RegistrationProviderId, tenantId: string | null, signal?: AbortSignal) {
  return api<RegistrationAccount>(`/v1/me/registration-accounts/${executor}`, { tenantId, signal });
}
export function saveRegistrationAccount(executor: RegistrationProviderId, values: RegistrationAccountValues, revision: number, tenantId: string | null) {
  return api<RegistrationAccount>(`/v1/me/registration-accounts/${executor}`, {
    method: "PUT", tenantId, bodyJson: { ...values, revision },
  });
}

/** Practical ASCII email syntax; the API repeats validation before storing metadata. */
export function validateRegistrationEmail(raw: string): string | null {
  const value = raw.replace(/^ +| +$/g, "");
  if (!value) return null;
  if (!/^[\x21-\x7e]+$/.test(value)) return "Enter a valid email address.";
  if (value.length > 254) return "Use 254 characters or fewer.";
  const parts = value.split("@");
  if (parts.length !== 2) return "Enter a valid email address.";
  const [local, domain] = parts;
  if (local.length > 64 || !/^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$/.test(local)) return "Enter a valid email address.";
  const labels = domain.split(".");
  if (labels.length < 2 || labels.some(label => label.length > 63 || !/^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$/.test(label))
    || !/^[A-Za-z]{2,}$/.test(labels.at(-1) ?? "")) return "Enter a valid email address.";
  return null;
}
