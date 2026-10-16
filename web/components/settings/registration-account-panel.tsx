"use client";

import { ChevronDown } from "lucide-react";
import Link from "next/link";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { useSession } from "@/components/session-provider";
import { SettingsActions, SettingsSaveButton } from "@/components/settings/settings-actions";
import { SettingsHelp } from "@/components/settings/settings-help";
import { ApiError, readableError } from "@/lib/api";
import type { RegistrationProviderId } from "@/lib/registration-providers";
import {
  EMPTY_REGISTRATION_ACCOUNT, REGISTRATION_SITES, getRegistrationAccount, registrationAccountDraft,
  registrationAccountValues, saveRegistrationAccount, validateRegistrationEmail,
  type RegistrationAccount, type RegistrationAccountDraft,
} from "@/lib/registration-accounts";
import styles from "./muse-panel.module.css";

type FieldErrors = Partial<Record<keyof RegistrationAccountDraft, string>>;

export function RegistrationAccountPanel({ executor, children }: { executor: RegistrationProviderId; children?: ReactNode }) {
  const formId = useId();
  const { tenantId } = useSession();
  const [account, setAccount] = useState<RegistrationAccount | null>(null);
  const [draft, setDraft] = useState<RegistrationAccountDraft>(EMPTY_REGISTRATION_ACCOUNT);
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);

  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    setAccount(null); setDraft(EMPTY_REGISTRATION_ACCOUNT); setLoading(true);
    setSaving(false); setSaved(false); setConflict(false); setError(null); setErrors({});
    getRegistrationAccount(executor, tenantId, controller.signal).then(value => {
      if (current !== generation.current) return;
      setAccount(value); setDraft(registrationAccountDraft(value));
      setExpanded(Object.keys(value.provider_overrides).length > 0);
    }).catch(problem => {
      if (current === generation.current && !controller.signal.aborted) setError(readableError(problem, "Could not load the registration account."));
    }).finally(() => { if (current === generation.current) setLoading(false); });
    return () => { controller.abort(); generation.current++; };
  }, [executor, tenantId]);

  const values = registrationAccountValues(draft);
  const dirty = account !== null && (values.expected_email !== account.expected_email
    || REGISTRATION_SITES.some(site => values.provider_overrides[site.id] !== account.provider_overrides[site.id]));

  function edit(field: keyof RegistrationAccountDraft, value: string) {
    setDraft(current => ({ ...current, [field]: value })); setSaved(false);
    setErrors(current => ({ ...current, [field]: undefined }));
    if (!conflict) setError(null);
  }
  async function reload() {
    const current = generation.current;
    setLoading(true); setSaved(false); setError(null);
    try {
      const value = await getRegistrationAccount(executor, tenantId);
      if (current !== generation.current) return;
      setAccount(value); setConflict(false);
      // A conflicting save keeps the reader's edits available for comparison and retry.
      if (!account) { setDraft(registrationAccountDraft(value)); setExpanded(Object.keys(value.provider_overrides).length > 0); }
    } catch (problem) {
      if (current === generation.current) setError(readableError(problem, "Could not reload the registration account."));
    } finally { if (current === generation.current) setLoading(false); }
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!account || saving || loading || conflict) return;
    const problems: FieldErrors = {};
    for (const field of ["defaultEmail", ...REGISTRATION_SITES.map(site => site.id)] as const) {
      const problem = validateRegistrationEmail(draft[field]);
      if (problem) problems[field] = problem;
    }
    setErrors(problems);
    if (Object.keys(problems).length) { if (REGISTRATION_SITES.some(site => problems[site.id])) setExpanded(true); return; }
    const current = generation.current;
    setSaving(true); setSaved(false); setError(null);
    try {
      const value = await saveRegistrationAccount(executor, registrationAccountValues(draft), account.revision + 1, tenantId);
      if (current !== generation.current) return;
      setAccount(value); setDraft(registrationAccountDraft(value)); setSaved(true);
    } catch (problem) {
      if (current !== generation.current) return;
      if (problem instanceof ApiError && problem.status === 409) {
        setConflict(true); setError("Settings changed. Reload and review before saving.");
      } else setError(readableError(problem, "Your registration account was not saved."));
    } finally { if (current === generation.current) setSaving(false); }
  }
  const disabled = !account || loading || saving;
  return <div className="settings-panel">
    <form id={formId} className="settings-card" aria-labelledby="registration-account-heading" noValidate onSubmit={submit}>
      <div className={styles.fieldLabel}>
        <h2 id="registration-account-heading" className="settings-card__title">Registration account</h2>
        <SettingsHelp label="Registration account">Can differ from your Events Concierge sign-in. Used for provider sign-in and signup email, including company email when accepted. Partiful uses phone login; you complete phone verification. Its email setting applies to form fields. An empty default uses the current browser account; an empty site override uses the default. Changes apply to queued registrations; active attempts keep their saved email.</SettingsHelp>
      </div>
      <div className="settings-field">
        <label htmlFor="registration-default-email">Default email</label>
        <input id="registration-default-email" type="email" autoComplete="off" maxLength={254}
          placeholder="Current browser account" value={draft.defaultEmail} disabled={disabled}
          aria-invalid={Boolean(errors.defaultEmail)} aria-describedby={errors.defaultEmail ? "registration-default-email-error" : undefined}
          onChange={event => edit("defaultEmail", event.target.value)} />
        {errors.defaultEmail ? <p id="registration-default-email-error" className="settings-error" role="alert">{errors.defaultEmail}</p> : null}
      </div>
      <button type="button" className={styles.overrideToggle} aria-expanded={expanded} aria-controls="registration-site-overrides"
        disabled={disabled} onClick={() => setExpanded(value => !value)}>Per site <ChevronDown aria-hidden="true" /></button>
      {expanded ? <div id="registration-site-overrides" className={styles.overrides}>
        {REGISTRATION_SITES.map(site => <div className="settings-field" key={site.id}>
          <label htmlFor={`registration-${site.id}-email`}>{site.label} email</label>
          <input id={`registration-${site.id}-email`} type="email" autoComplete="off" maxLength={254} placeholder="Default account"
            value={draft[site.id]} disabled={disabled} aria-invalid={Boolean(errors[site.id])}
            aria-describedby={errors[site.id] ? `registration-${site.id}-error` : undefined}
            onChange={event => edit(site.id, event.target.value)} />
          {errors[site.id] ? <p id={`registration-${site.id}-error`} className="settings-error" role="alert">{errors[site.id]}</p> : null}
        </div>)}
      </div> : null}
      {error ? <p className="settings-error" role="alert">{error}</p> : null}
    </form>
    {children}
    <SettingsActions
      leading={<Link className={styles.link} href="/docs/muse#account">Account guide</Link>}
      status={loading ? "Loading…" : saved ? "Saved." : dirty ? "Unsaved changes." : ""}>
      {conflict || (!account && error) ? <button type="button" className="button" disabled={loading || saving} onClick={() => void reload()}>Reload</button> : null}
      <SettingsSaveButton type="submit" form={formId} saving={saving} disabled={disabled || !dirty || conflict} />
    </SettingsActions>
  </div>;
}
