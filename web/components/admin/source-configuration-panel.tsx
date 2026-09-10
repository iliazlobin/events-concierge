"use client";

import { RefreshCw, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { getAdminSourceDetail } from "@/lib/admin-api";
import type { AdminSourceConfiguration, AdminSourceConfigurationUpdate } from "@/lib/admin-types";

import { age, stamp } from "./console-kit";
import { SourceConfigurationEditor } from "./source-configuration-editor";
import { useAdminSnapshot } from "./use-admin-snapshot";
import styles from "./source-configuration-panel.module.css";

export interface SourceConfigurationPanelProps {
  id?: string;
  embedded?: boolean;
  sourceKey: string;
  canConfigure: boolean;
  refreshVersion: number;
  onSaved: (result: AdminSourceConfigurationUpdate) => void;
  onClose: () => void;
  onDirtyChange?: (dirty: boolean) => void;
  onSavingChange?: (saving: boolean) => void;
  includeFixtures?: boolean;
  onAuthorizationDeniedChange?: (denied: boolean) => void;
  disabled?: boolean;
}

/** Exact registry evidence and the shared reviewed editor; roster summaries never authorize edits. */
export function SourceConfigurationPanel({
  id, embedded = false,
  sourceKey, canConfigure, refreshVersion, onSaved, onClose,
  onDirtyChange, onSavingChange, onAuthorizationDeniedChange, includeFixtures = false, disabled: externalDisabled = false,
}: SourceConfigurationPanelProps) {
  const load = useCallback((signal: AbortSignal) => getAdminSourceDetail(sourceKey, 24, includeFixtures, signal), [sourceKey, includeFixtures]);
  const snapshot = useAdminSnapshot(load, refreshVersion);
  const source = !snapshot.authorizationDenied && snapshot.data?.source.source_key === sourceKey ? snapshot.data.source : null;
  const [draft, setDraft] = useState({ sourceKey, dirty: false, saving: false });
  const [pinnedSource, setPinnedSource] = useState<AdminSourceConfiguration | null>(null);
  const [editorEpoch, setEditorEpoch] = useState(0);
  const [pendingRevision, setPendingRevision] = useState<{ sourceKey: string; revision: number } | null>(null);
  const revisionSourceKey = useRef(sourceKey);
  const mounted = useRef(true);
  const current = useRef({ sourceKey, onSaved, onDirtyChange, onSavingChange });
  current.current = { sourceKey, onSaved, onDirtyChange, onSavingChange };
  const dirty = draft.sourceKey === sourceKey && draft.dirty;
  const saving = draft.sourceKey === sourceKey && draft.saving;
  const editorSource = (dirty || saving) && pinnedSource?.source_key === sourceKey ? pinnedSource : source;
  const revisionChanged = Boolean(source && editorSource && source.source_revision !== editorSource.source_revision);
  const verifyingSave = Boolean(source && pendingRevision?.sourceKey === sourceKey && source.source_revision < pendingRevision.revision);
  const disabled = externalDisabled || snapshot.loading || snapshot.failed || snapshot.authorizationDenied || !source || revisionChanged || verifyingSave;

  useEffect(() => {
    if (source && !dirty && !saving) setPinnedSource(source);
  }, [source, dirty, saving]);
  useEffect(() => {
    if (revisionSourceKey.current === sourceKey) return;
    revisionSourceKey.current = sourceKey;
    setPendingRevision(null);
  }, [sourceKey]);
  useEffect(() => {
    if (snapshot.authorizationDenied) {
      setDraft({ sourceKey, dirty: false, saving: false });
      setPinnedSource(null);
    }
  }, [snapshot.authorizationDenied, sourceKey]);
  useEffect(() => { current.current.onDirtyChange?.(dirty); }, [dirty]);
  useEffect(() => { current.current.onSavingChange?.(saving); }, [saving]);
  useEffect(() => { onAuthorizationDeniedChange?.(snapshot.authorizationDenied); }, [snapshot.authorizationDenied, onAuthorizationDeniedChange]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      current.current.onDirtyChange?.(false);
      current.current.onSavingChange?.(false);
    };
  }, []);

  const reportDirty = useCallback((next: boolean) => {
    if (!mounted.current || current.current.sourceKey !== sourceKey) return;
    setDraft((value) => ({ sourceKey, dirty: next, saving: value.sourceKey === sourceKey && value.saving }));
  }, [sourceKey]);
  const reportSaving = useCallback((next: boolean) => {
    if (!mounted.current || current.current.sourceKey !== sourceKey) return;
    setDraft((value) => ({ sourceKey, dirty: value.sourceKey === sourceKey && value.dirty, saving: next }));
  }, [sourceKey]);
  const saved = (result: AdminSourceConfigurationUpdate) => {
    // Accepted writes invalidate the registry even if another source is now selected.
    current.current.onSaved(result);
    if (!mounted.current || result.source_key !== current.current.sourceKey) return;
    reportDirty(false);
    setPendingRevision({ sourceKey: result.source_key, revision: result.source_revision });
    void snapshot.refresh();
  };
  const discardDraft = () => {
    if (saving || externalDisabled || !source || snapshot.loading || snapshot.failed || snapshot.authorizationDenied) return;
    setPinnedSource(source);
    reportDirty(false);
    setEditorEpoch((value) => value + 1);
  };
  return <section className={`${styles.panel}${embedded ? ` ${styles.embedded}` : ""}`} id={id ?? "source-inline-configuration"} aria-label="Source configuration">
    {!embedded ? <><div className={styles.header}>
      <h2>Configuration</h2>
      <div><button type="button" onClick={() => void snapshot.refresh()} disabled={snapshot.loading || saving} aria-label="Refresh configuration">
        <RefreshCw aria-hidden="true" className={snapshot.loading ? "spin" : undefined} /></button>
        <button type="button" onClick={onClose} disabled={saving} aria-label="Close configuration"><X aria-hidden="true" /></button></div>
    </div>
    <h3 className={styles.sourceName}>{source?.display_name ?? sourceKey}</h3>
    <code className={styles.sourceKey}>{sourceKey}</code></> : null}
    {source ? <div className={styles.context}>
      {!embedded ? <span>Current revision {source.source_revision}</span> : null}
      <span title={stamp(snapshot.data?.generated_at)}>{snapshot.loading ? "Reading configuration…" : snapshot.failed ? "Last successful snapshot" : `Snapshot ${age(snapshot.data?.generated_at)}`}</span>
    </div> : null}

    {snapshot.authorizationDenied ? <div className={styles.empty} role="alert">
      <p>Source configuration access was denied. The previous configuration and draft are no longer displayed.</p>
      <button type="button" onClick={() => void snapshot.refresh()}>Retry</button>
    </div> : !source ? <div className={styles.empty} role={snapshot.failed ? "alert" : "status"}>
      <p>{snapshot.failed ? "This source configuration could not be read." : "Reading the selected source configuration…"}</p>
      {snapshot.failed ? <button type="button" onClick={() => void snapshot.refresh()}>Retry</button> : null}
    </div> : <>
      {snapshot.failed ? <div className={styles.notice} role="alert">
        <p>The latest configuration read failed. Retained fields are read-only until a successful read; any draft is preserved.</p>
        <button type="button" onClick={() => void snapshot.refresh()}>Retry</button>
      </div> : null}
      {revisionChanged ? <div className={styles.notice} role="status">
        <p>Revision {source.source_revision} is now recorded. Your draft still uses revision {editorSource?.source_revision}; saving is paused. Cancel to use the latest revision.</p>
      </div> : null}
      {verifyingSave ? <p className={styles.note} role="status">Revision {pendingRevision?.revision} was saved. Reading the current configuration before editing again.</p> : null}
      {dirty && !revisionChanged ? <p className={styles.draftNote} role="status">Unsaved draft · background reads preserve your fields. Closing or changing sources discards it.</p> : null}
      {!canConfigure ? <p className={styles.intro}>Saving reviewed changes requires reviewer access.</p> : null}
      <fieldset className={styles.editor} aria-label="Source configuration fields" onSubmitCapture={(event) => {
        if (disabled || !canConfigure) { event.preventDefault(); event.stopPropagation(); }
      }}>
        {editorSource ? <SourceConfigurationEditor key={`${sourceKey}:${editorEpoch}`} source={editorSource}
          canConfigure={canConfigure} disabled={disabled} onSaved={saved}
          onCancel={discardDraft} canCancel={!externalDisabled && !snapshot.loading && !snapshot.failed && Boolean(source)}
          onConflict={() => void snapshot.refresh()}
          onDirtyChange={reportDirty} onSavingChange={reportSaving} /> : null}
      </fieldset>
    </>}
  </section>;
}
