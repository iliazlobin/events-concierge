"use client";

import {
  ArrowUpRight,
  ChevronRight,
  CircleAlert,
  LoaderCircle,
  Save,
  X,
} from "lucide-react";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import { updateAdminSourceConfiguration } from "@/lib/admin-api";
import {
  formatFullDate,
  formatNumber,
  humanize,
  safeHttpUrl,
} from "@/lib/admin-presentation";
import { adminSourceIsRetired } from "@/lib/admin-source-lifecycle";
import { collectionHorizonDays, collectionHorizonLabel } from "@/lib/admin-source-configuration";
import type {
  AdminSourceConfiguration,
  AdminSourceConfigurationUpdate,
} from "@/lib/admin-types";
import { ApiError } from "@/lib/api";
import styles from "./source-configuration-editor.module.css";

interface SourceConfigurationEditorProps {
  canConfigure: boolean;
  source: AdminSourceConfiguration;
  onSaved: (result: AdminSourceConfigurationUpdate) => void;
  disabled?: boolean;
  onDirtyChange?: (dirty: boolean) => void;
  onSavingChange?: (saving: boolean) => void;
  onCancel?: () => void;
  canCancel?: boolean;
  onConflict?: () => void;
}

interface FormState {
  seedUrl: string;
  enabled: boolean;
  refreshIntervalMinutes: string;
  minIntervalMs: string;
  pageLimit: string;
  collectionHorizonDays: string;
  reviewExpiresAt: string;
  approvedOrigins: string;
}

const CADENCE_PRESETS = [
  { label: "Daily", minutes: 1_440 },
  { label: "Every 12h", minutes: 720 },
  { label: "Every 6h", minutes: 360 },
] as const;
const EVENT_WINDOW_PRESETS = [30, 60, 90] as const;

function ConfigurationLink({ value }: { value: string }) {
  const href = safeHttpUrl(value);
  return href ? <a className={styles.sourceLink} href={href} target="_blank" rel="noopener noreferrer">
    <span>{value}</span><ArrowUpRight aria-hidden="true" />
  </a> : <span>{value}</span>;
}

function ManagedConfiguration({ source }: { source: AdminSourceConfiguration }) {
  return <details className="admin-config-section admin-config-section--managed">
          <summary className="admin-config-section__heading">
            <div>
              <strong>Identity, adapter, and policy</strong>
              <span>System-managed provenance and safety constraints are read-only here.</span>
            </div>
            <span className="admin-config-section__summary-meta">
              <small>Managed</small>
              <ChevronRight aria-hidden="true" />
            </span>
          </summary>
          <dl className="admin-definition-grid">
            <div><dt>Display name</dt><dd>{source.display_name}</dd></div>
            <div><dt>Source key</dt><dd className="is-code">{source.source_key}</dd></div>
            <div><dt>Publisher</dt><dd>{source.publisher}</dd></div>
            <div><dt>Region</dt><dd>{humanize(source.region)}</dd></div>
            <div><dt>Adapter contract</dt><dd>{humanize(source.mode)}</dd></div>
            <div><dt>Derived seed host</dt><dd className="is-code">{source.seed_host}</dd></div>
            <div><dt>Discovery posture</dt><dd>{source.handoff_only ? "Handoff-only · required" : "Invalid posture"}</dd></div>
            <div><dt>Owner review</dt><dd>{formatFullDate(source.reviewed_at)}</dd></div>
            <div><dt>Registry revision</dt><dd>{formatNumber(source.source_revision)}</dd></div>
            <div><dt>Review state</dt><dd>{humanize(source.review_status)}</dd></div>
          </dl>
        </details>;
}

function localDateTime(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function initialState(source: AdminSourceConfiguration): FormState {
  return {
    seedUrl: source.seed_url,
    enabled: source.enabled,
    refreshIntervalMinutes: String(source.refresh_interval_minutes),
    minIntervalMs: String(source.min_interval_ms),
    pageLimit: String(source.page_limit),
    collectionHorizonDays: collectionHorizonDays(source.collection_horizon_days)?.toString() ?? "",
    reviewExpiresAt: localDateTime(source.review_expires_at),
    approvedOrigins: source.approved_origins.join("\n"),
  };
}

function readableError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409) {
      return "This source changed while you were editing. Reload the latest revision and retry.";
    }
    return error.message;
  }
  return error instanceof Error ? error.message : "Could not update this source.";
}

function validConfiguration(form: FormState): boolean {
  const integer = (value: string, min: number, max: number) => /^\d+$/.test(value) && Number(value) >= min && Number(value) <= max;
  if (!integer(form.refreshIntervalMinutes, 5, 1_440) || !integer(form.minIntervalMs, 250, 60_000)
    || !integer(form.pageLimit, 1, 500) || form.seedUrl.trim().length > 2_048
    || (form.reviewExpiresAt && !Number.isFinite(new Date(form.reviewExpiresAt).getTime()))) return false;
  const origins = form.approvedOrigins.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
  if (origins.length < 1 || origins.length > 20) return false;
  try {
    const seed = new URL(form.seedUrl.trim());
    const urls = origins.map(value => new URL(value));
    return seed.protocol === "https:" && Boolean(seed.hostname)
      && urls.every(url => url.protocol === "https:" && Boolean(url.hostname))
      && new Set(urls.map(url => url.origin)).size === urls.length
      && urls.some(url => url.origin === seed.origin);
  } catch { return false; }
}

export function SourceConfigurationEditor({
  canConfigure,
  source,
  onSaved,
  onCancel,
  canCancel = true,
  onConflict,
  disabled = false,
  onDirtyChange,
  onSavingChange,
}: SourceConfigurationEditorProps) {
  const [form, setForm] = useState<FormState>(() => initialState(source));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const retired = adminSourceIsRetired(source);
  const supportsEventWindow = collectionHorizonDays(source.collection_horizon_days) !== null;
  const validEventWindow = !supportsEventWindow || collectionHorizonDays(Number(form.collectionHorizonDays)) !== null;
  const generation = useRef(0);
  const mounted = useRef(true);
  const formIdentity = useRef({ sourceKey: source.source_key, revision: source.source_revision });
  const [conflict, setConflict] = useState(false);
  const validRevision = Number.isSafeInteger(source.source_revision) && source.source_revision > 0;

  useEffect(() => {
    // React may reconnect effects when a retained source row moves. Only a new
    // source/revision resets fields; replaying the same read must preserve a draft.
    if (formIdentity.current.sourceKey === source.source_key
      && formIdentity.current.revision === source.source_revision) return;
    formIdentity.current = { sourceKey: source.source_key, revision: source.source_revision };
    generation.current += 1;
    setForm(initialState(source));
    setSaving(false);
    setError(null);
    setConflict(false);
  }, [source.source_key, source.source_revision]);

  useEffect(() => {
    if (canConfigure) return;
    generation.current += 1;
    setForm(initialState(source));
    setError(null);
    setSaving(false);
    setConflict(false);
  }, [canConfigure]);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const baseline = useMemo(() => initialState(source), [source]);
  const dirty = (
    form.seedUrl.trim() !== baseline.seedUrl
    || form.enabled !== baseline.enabled
    || form.refreshIntervalMinutes !== baseline.refreshIntervalMinutes
    || form.minIntervalMs !== baseline.minIntervalMs
    || form.pageLimit !== baseline.pageLimit
    || (supportsEventWindow && form.collectionHorizonDays !== baseline.collectionHorizonDays)
    || form.reviewExpiresAt !== baseline.reviewExpiresAt
    || form.approvedOrigins.trim() !== baseline.approvedOrigins
  );
  useEffect(() => { onDirtyChange?.(canConfigure && !retired && dirty); }, [canConfigure, retired, dirty, onDirtyChange]);
  useEffect(() => { onSavingChange?.(saving); }, [saving, onSavingChange]);

  const validForm = validConfiguration(form) && validEventWindow;
  const cancel = () => {
    if (saving || !canCancel) return;
    if (onCancel) onCancel();
    else { setForm(initialState(source)); setError(null); }
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canConfigure || disabled || retired || saving || conflict || !validRevision || !dirty || !validForm || !event.currentTarget.checkValidity()) return;
    const submittedGeneration = generation.current;
    const approvedOrigins = form.approvedOrigins
      .split(/\r?\n/)
      .map((origin) => origin.trim())
      .filter(Boolean);
    setSaving(true);
    setError(null);
    try {
      const updated = await updateAdminSourceConfiguration(source.source_key, {
        expected_revision: source.source_revision,
        seed_url: form.seedUrl.trim(),
        approved_origins: approvedOrigins,
        mode: source.mode,
        enabled: form.enabled,
        handoff_only: true,
        review_expires_at: form.reviewExpiresAt
          ? new Date(form.reviewExpiresAt).toISOString()
          : null,
        refresh_interval_minutes: Number(form.refreshIntervalMinutes),
        min_interval_ms: Number(form.minIntervalMs),
        page_limit: Number(form.pageLimit),
        ...(supportsEventWindow ? { collection_horizon_days: Number(form.collectionHorizonDays) } : {}),
        review_acknowledged: true,
      });
      onSaved(updated);
    } catch (nextError) {
      if (mounted.current && generation.current === submittedGeneration) {
        setError(readableError(nextError));
        if (nextError instanceof ApiError && nextError.status === 409) {
          setConflict(true);
          onConflict?.();
        }
      }
    } finally {
      if (mounted.current && generation.current === submittedGeneration) setSaving(false);
    }
  };

  if (!canConfigure || retired) {
    return (
      <div className="admin-config">
        <div className="admin-config__toolbar">
          <div>
            <code>registry.rev/{source.source_revision}</code>
            <span>{retired ? "retired" : source.enabled ? "enabled" : "disabled"}</span>
          </div>

        </div>

        <section className="admin-config-section">
          <div className="admin-config-section__heading">
            <div>
              <strong>Reviewed operational fields</strong>
              <span>{retired
                ? "Historical configuration retained; retired sources are read-only here."
                : "Current reviewed settings and source links."}</span>
            </div>
            <small>{retired ? "Retired · read-only" : "Read-only · reviewer access required"}</small>
          </div>
          <dl className="admin-definition-grid">
            <div className="admin-definition-grid__wide">
              <dt className={styles.fieldLabel}><span>Seed URL</span></dt>
              <dd className="is-code"><ConfigurationLink value={source.seed_url} /></dd>
            </div>
            <div><dt className={styles.fieldLabel}><span>Collection state</span></dt><dd>{retired ? "Retired" : source.enabled ? "Enabled" : "Disabled"}</dd></div>
            <div>
              <dt className={styles.fieldLabel}><span>Refresh cadence</span></dt>
              <dd>{CADENCE_PRESETS.find((preset) => preset.minutes === source.refresh_interval_minutes)?.label ?? "Custom"} · {formatNumber(source.refresh_interval_minutes)} min</dd>
            </div>
            <div><dt className={styles.fieldLabel}><span>Minimum pacing</span></dt><dd>{formatNumber(source.min_interval_ms)} ms</dd></div>
            <div><dt className={styles.fieldLabel}><span>Page limit</span></dt><dd>{formatNumber(source.page_limit)}</dd></div>
            <div>
              <dt className={styles.fieldLabel}><span>Event window</span></dt>
              <dd>{collectionHorizonLabel(source.collection_horizon_days)}</dd>
              {!supportsEventWindow ? <p className={styles.unavailable}>Editing requires a backend that reports this setting.</p> : null}
            </div>
            <div><dt className={styles.fieldLabel}><span>Review expires</span></dt><dd>{formatFullDate(source.review_expires_at)}</dd></div>
            {retired ? (
              <>
                <div><dt>Retired</dt><dd>{formatFullDate(source.retired_at)}</dd></div>
                <div><dt>Retirement reason</dt><dd>{humanize(source.retired_reason ?? "retired")}</dd></div>
                {source.superseded_by_source_key ? (
                  <div className="admin-definition-grid__wide">
                    <dt>Replacement source</dt>
                    <dd className="is-code">{source.superseded_by_source_key}</dd>
                  </div>
                ) : null}
              </>
            ) : null}
            <div className="admin-definition-grid__wide">
              <dt className={styles.fieldLabel}><span>Approved origins</span></dt>
              <dd className={`is-code ${styles.origins}`}>{source.approved_origins.length
                ? source.approved_origins.map((origin) => <ConfigurationLink key={origin} value={origin} />)
                : "None recorded"}</dd>
            </div>
          </dl>
        </section>

        <ManagedConfiguration source={source} />
      </div>
    );
  }

  return (
    <form className="admin-config-form" onSubmit={(event) => void submit(event)}>
      <div className="admin-config-form__toolbar">
        <code>registry.rev/{validRevision ? source.source_revision : "unknown"}</code>
        <span>{dirty ? "Unsaved changes" : "Current settings"}</span>
      </div>
      {!validRevision ? <p role="alert">A verified registry revision is required before saving.</p> : null}
      <fieldset disabled={disabled || saving || conflict || !validRevision} className={styles.fields}>
      <div className="admin-config-form__grid">
        <label className="is-wide">
          <span>Seed URL</span>
          <input
            name="seedUrl"
            type="url"
            required
            maxLength={2_048}
            value={form.seedUrl}
            onChange={(event) => setForm({ ...form, seedUrl: event.target.value })}
          />
        </label>
        <label className="admin-config-toggle">
          <span>Collection state</span>
          <button
            name="enabled"
            type="button"
            role="switch"
            aria-label="Collection state"
            aria-checked={form.enabled}
            className={form.enabled ? "is-on" : ""}
            onClick={() => setForm({ ...form, enabled: !form.enabled })}
          >
            <i />
            {form.enabled ? "Enabled" : "Disabled"}
          </button>
        </label>
        <div className={styles.cadence}>
          <div className={styles.presets} role="group" aria-label="Refresh cadence presets">
            {CADENCE_PRESETS.map((preset) => <button key={preset.minutes} type="button"
              aria-pressed={form.refreshIntervalMinutes === String(preset.minutes)}
              onClick={() => setForm({ ...form, refreshIntervalMinutes: String(preset.minutes) })}>{preset.label}</button>)}
          </div>
          <label>
            <span>Refresh interval · min</span>
            <input
              name="refreshIntervalMinutes"
              type="number"
              required
              min={5}
              max={1_440}
              value={form.refreshIntervalMinutes}
              onChange={(event) => setForm({
                ...form,
                refreshIntervalMinutes: event.target.value,
              })}
            />
          </label>
          <small>Choose a preset or enter 5–1,440 minutes. Measured from the last successful completion.</small>
        </div>
        <label>
          <span>Minimum pacing · ms</span>
          <input
            name="minIntervalMs"
            type="number"
            required
            min={250}
            max={60_000}
            value={form.minIntervalMs}
            onChange={(event) => setForm({ ...form, minIntervalMs: event.target.value })}
          />
        </label>
        <label>
          <span>Page limit</span>
          <input
            name="pageLimit"
            type="number"
            required
            min={1}
            max={500}
            value={form.pageLimit}
            onChange={(event) => setForm({ ...form, pageLimit: event.target.value })}
          />
        </label>
        <label>
          <span>Review expires · optional</span>
          <input
            name="reviewExpiresAt"
            type="datetime-local"
            value={form.reviewExpiresAt}
            onChange={(event) => setForm({ ...form, reviewExpiresAt: event.target.value })}
          />
        </label>
        <div className={styles.cadence}>
          <div className={styles.presets} role="group" aria-label="Event window presets">
            {EVENT_WINDOW_PRESETS.map((days) => <button key={days} type="button" disabled={!supportsEventWindow}
              aria-pressed={form.collectionHorizonDays === String(days)}
              onClick={() => setForm({ ...form, collectionHorizonDays: String(days) })}>{days} days</button>)}
          </div>
          <label>
            <span>Event window · days</span>
            <input name="collectionHorizonDays" type="number" min={1} max={90} required={supportsEventWindow}
              disabled={!supportsEventWindow} placeholder="Not recorded" value={form.collectionHorizonDays}
              onChange={(event) => setForm({ ...form, collectionHorizonDays: event.target.value })} />
          </label>
          <small>{supportsEventWindow ? "Collect events starting within this many upcoming days. Existing catalog records are retained."
            : "This backend does not report event-window settings yet. Other fields can still be saved."}</small>
        </div>
        <label className="is-wide">
          <span>Approved HTTPS origins · one per line</span>
          <textarea
            name="approvedOrigins"
            required
            rows={Math.max(2, source.approved_origins.length)}
            value={form.approvedOrigins}
            onChange={(event) => setForm({ ...form, approvedOrigins: event.target.value })}
          />
        </label>
      </div>

      {error ? (
        <div className="admin-inline-error" role="alert">
          <CircleAlert aria-hidden="true" />
          {error}
        </div>
      ) : null}

      </fieldset>
      <div className="admin-config-form__actions">
        <p className={styles.saveNote}>Save confirms review of the endpoint, origins and collection settings.</p>
        <button type="button" onClick={cancel} disabled={saving || !canCancel || (!dirty && !error)}>
          <X aria-hidden="true" />Cancel
        </button>
        <button className="admin-primary-button" type="submit"
          disabled={disabled || saving || conflict || !validRevision || !dirty || !validForm}>
          {saving ? <LoaderCircle className="spin" aria-hidden="true" /> : <Save aria-hidden="true" />}
          {saving ? "Saving…" : "Save"}
        </button>
      </div>
      <ManagedConfiguration source={source} />
    </form>
  );
}
