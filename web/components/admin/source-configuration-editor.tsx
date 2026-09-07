"use client";

import {
  Check,
  ChevronRight,
  CircleAlert,
  LoaderCircle,
  Pencil,
  RotateCcw,
  Save,
  ShieldCheck,
  X,
} from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";

import { updateAdminSourceConfiguration } from "@/lib/admin-api";
import {
  formatFullDate,
  formatNumber,
  humanize,
} from "@/lib/admin-presentation";
import { adminSourceIsRetired } from "@/lib/admin-source-lifecycle";
import type {
  AdminSourceConfiguration,
  AdminSourceConfigurationUpdate,
} from "@/lib/admin-types";
import { ApiError } from "@/lib/api";

interface SourceConfigurationEditorProps {
  source: AdminSourceConfiguration;
  onSaved: (result: AdminSourceConfigurationUpdate) => void;
  editRequest?: {
    sourceKey: string;
    nonce: number;
  } | null;
}

interface FormState {
  seedUrl: string;
  enabled: boolean;
  refreshIntervalMinutes: string;
  minIntervalMs: string;
  pageLimit: string;
  reviewExpiresAt: string;
  approvedOrigins: string;
  acknowledged: boolean;
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
    reviewExpiresAt: localDateTime(source.review_expires_at),
    approvedOrigins: source.approved_origins.join("\n"),
    acknowledged: false,
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

export function SourceConfigurationEditor({
  source,
  onSaved,
  editRequest = null,
}: SourceConfigurationEditorProps) {
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<FormState>(() => initialState(source));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const retired = adminSourceIsRetired(source);

  useEffect(() => {
    setForm(initialState(source));
    setEditing(false);
    setSaving(false);
    setError(null);
  }, [source.source_key, source.source_revision]);

  useEffect(() => {
    if (retired || editRequest?.sourceKey !== source.source_key) return;
    setError(null);
    setEditing(true);
  }, [editRequest?.nonce, editRequest?.sourceKey, retired, source.source_key]);

  const baseline = useMemo(() => initialState(source), [source]);
  const dirty = (
    form.seedUrl.trim() !== baseline.seedUrl
    || form.enabled !== baseline.enabled
    || form.refreshIntervalMinutes !== baseline.refreshIntervalMinutes
    || form.minIntervalMs !== baseline.minIntervalMs
    || form.pageLimit !== baseline.pageLimit
    || form.reviewExpiresAt !== baseline.reviewExpiresAt
    || form.approvedOrigins.trim() !== baseline.approvedOrigins
  );

  const cancel = () => {
    if (saving) return;
    setForm(initialState(source));
    setError(null);
    setEditing(false);
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (retired || saving || !dirty || !form.acknowledged) return;
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
        review_acknowledged: true,
      });
      onSaved(updated);
      setEditing(false);
    } catch (nextError) {
      setError(readableError(nextError));
    } finally {
      setSaving(false);
    }
  };

  if (!editing) {
    return (
      <div className="admin-config">
        <div className="admin-config__toolbar">
          <div>
            <code>registry.rev/{source.source_revision}</code>
            <span>{retired ? "retired" : source.enabled ? "enabled" : "disabled"}</span>
          </div>
          {!retired ? (
            <button type="button" onClick={() => setEditing(true)}>
              <Pencil aria-hidden="true" />
              Edit reviewed config
            </button>
          ) : null}
        </div>

        <section className="admin-config-section">
          <div className="admin-config-section__heading">
            <div>
              <strong>Reviewed operational fields</strong>
              <span>{retired
                ? "Immutable historical configuration retained for operator evidence."
                : "Editable settings; each save writes an audited registry revision."}</span>
            </div>
            <small>{retired ? "Retired · read-only" : "Editable · audited"}</small>
          </div>
          <dl className="admin-definition-grid">
            <div className="admin-definition-grid__wide">
              <dt>Seed URL</dt>
              <dd className="is-code">{source.seed_url}</dd>
            </div>
            <div><dt>Collection state</dt><dd>{retired ? "Retired" : source.enabled ? "Enabled" : "Disabled"}</dd></div>
            <div>
              <dt>Refresh cadence</dt>
              <dd>Every {formatNumber(source.refresh_interval_minutes)} min</dd>
            </div>
            <div><dt>Minimum pacing</dt><dd>{formatNumber(source.min_interval_ms)} ms</dd></div>
            <div><dt>Page limit</dt><dd>{formatNumber(source.page_limit)}</dd></div>
            <div><dt>Review expires</dt><dd>{formatFullDate(source.review_expires_at)}</dd></div>
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
              <dt>Approved origins</dt>
              <dd className="is-code">{source.approved_origins.join(", ") || "None recorded"}</dd>
            </div>
          </dl>
        </section>

        <details className="admin-config-section admin-config-section--managed">
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
        </details>
      </div>
    );
  }

  return (
    <form className="admin-config-form" onSubmit={(event) => void submit(event)}>
      <div className="admin-config-form__toolbar">
        <div>
          <strong>Editing registry revision {source.source_revision}</strong>
          <span>Saving re-reviews the source and writes an immutable audit record.</span>
        </div>
        <button type="button" onClick={cancel} disabled={saving} aria-label="Cancel editing">
          <X aria-hidden="true" />
        </button>
      </div>

      <div className="admin-config-form__managed" role="note">
        <ShieldCheck aria-hidden="true" />
        <div>
          <span>System-managed adapter contract</span>
          <strong>{source.display_name} · {humanize(source.mode)}</strong>
          <small>
            Source identity, publisher, region, adapter mode, and handoff-only policy are not
            changed by this form.
          </small>
        </div>
      </div>

      <div className="admin-config-form__grid">
        <label className="is-wide">
          <span>Seed URL</span>
          <input
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
            type="button"
            role="switch"
            aria-checked={form.enabled}
            className={form.enabled ? "is-on" : ""}
            onClick={() => setForm({ ...form, enabled: !form.enabled })}
          >
            <i />
            {form.enabled ? "Enabled" : "Disabled"}
          </button>
        </label>
        <label>
          <span>Refresh interval · min</span>
          <input
            type="number"
            required
            min={5}
            max={10_080}
            value={form.refreshIntervalMinutes}
            onChange={(event) => setForm({
              ...form,
              refreshIntervalMinutes: event.target.value,
            })}
          />
        </label>
        <label>
          <span>Minimum pacing · ms</span>
          <input
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
            type="datetime-local"
            value={form.reviewExpiresAt}
            onChange={(event) => setForm({ ...form, reviewExpiresAt: event.target.value })}
          />
        </label>
        <label className="is-wide">
          <span>Approved HTTPS origins · one per line</span>
          <textarea
            required
            rows={Math.max(2, source.approved_origins.length)}
            value={form.approvedOrigins}
            onChange={(event) => setForm({ ...form, approvedOrigins: event.target.value })}
          />
        </label>
      </div>

      <div className="admin-config-policy">
        <ShieldCheck aria-hidden="true" />
        <div>
          <strong>Discovery remains handoff-only</strong>
          <span>
            The adapter contract is fixed. No registration, RSVP, or authenticated provider action
            is enabled here.
          </span>
        </div>
        <Check aria-hidden="true" />
      </div>

      {error ? (
        <div className="admin-inline-error">
          <CircleAlert aria-hidden="true" />
          {error}
        </div>
      ) : null}

      <div className="admin-config-form__actions">
        <label>
          <input
            type="checkbox"
            checked={form.acknowledged}
            onChange={(event) => setForm({ ...form, acknowledged: event.target.checked })}
          />
          <span>I reviewed the endpoint, origins, collection state, pacing, and fetch bounds.</span>
        </label>
        <button type="button" onClick={() => setForm(initialState(source))} disabled={saving}>
          <RotateCcw aria-hidden="true" />
          Reset
        </button>
        <button
          className="admin-primary-button"
          type="submit"
          disabled={saving || !dirty || !form.acknowledged}
        >
          {saving ? <LoaderCircle className="spin" aria-hidden="true" /> : <Save aria-hidden="true" />}
          {saving ? "Saving…" : "Save reviewed config"}
        </button>
      </div>
    </form>
  );
}
