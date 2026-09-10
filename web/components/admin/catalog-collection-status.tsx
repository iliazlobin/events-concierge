"use client";

import { ArrowUpRight } from "lucide-react";
import { useMemo } from "react";
import { sourceCollectionTimestamp, summarizeSourceCollection } from "@/lib/admin-source-operations";
import { formatRelativeTime } from "@/lib/admin-presentation";
import { int } from "./console-kit";
import { useSourceOperations } from "./use-source-operations";
import { SourceCollectionSchedule } from "./source-collection-schedule";
import styles from "./catalog-collection-status.module.css";

interface Props {
  sourceKey: string;
  refreshToken: number | string | null;
  onOpenSource: (sourceKey: string) => void;
}

export function CatalogCollectionStatus({ sourceKey, refreshToken, onOpenSource }: Props) {
  const snapshot = useSourceOperations(false, refreshToken);
  const roster = snapshot.loading || snapshot.failed ? null : snapshot.data;
  const status = useMemo(() => summarizeSourceCollection(roster?.items ?? null, sourceKey, Date.now()), [roster, sourceKey]);
  const excluded = status.state === "excluded";
  const lifecycle = status.eligibility === "retired" ? "Retired"
    : status.eligibility === "paused" ? "Paused" : "Blocked";
  return <section className={styles.root} aria-label="Catalog collection status" aria-busy={snapshot.loading}>
    <div className={styles.heading}>
      <h2>Collection freshness</h2>
      <p>{status.selected?.display_name ?? (sourceKey || "All non-fixture sources")} · current source state, independent of event filters and chart window</p>
    </div>
    {status.state === "unknown" ? <div className={styles.unavailable} role="status">
      <span>{snapshot.loading ? "Loading collection status…" : snapshot.failed ? "Collection status unavailable."
        : sourceKey ? "Selected source is unavailable in the current registry snapshot." : "Collection status unavailable."}</span>
      {!snapshot.loading ? <button type="button" onClick={() => void snapshot.refresh()}>Retry collection status</button> : null}
    </div> : excluded ? <div className={styles.excluded}>
      <strong>{lifecycle}</strong>
      <span>{lifecycle === "Retired" ? "Collection has ended for this source." : lifecycle === "Paused" ? "Collection is paused for this source." : "This source is not eligible for collection."} No refresh eligibility is shown.</span>
      <button type="button" aria-label={`Manage source for ${status.selected!.display_name}`} onClick={() => onOpenSource(status.selected!.source_key)}>Manage source<ArrowUpRight aria-hidden="true" /></button>
      <dl className={styles.lastSuccess}><div><dt>Last successful collection</dt><dd>{status.selectedLastSuccessAt
        ? <time dateTime={status.selectedLastSuccessAt} title={sourceCollectionTimestamp(status.selectedLastSuccessAt)}>{formatRelativeTime(status.selectedLastSuccessAt)}</time>
        : status.successDatesUnknown ? "Unknown" : "None recorded"}</dd></div></dl>
    </div> : <div className={styles.body}>
      <div>
        <dl className={styles.facts}>
          <div><dt title="Eligible sources recorded due, excluding sources already collecting.">Due for refresh</dt><dd>{int(status.dueCount!)}</dd></div>
          <div><dt title="Eligible sources recorded as running; worker liveness is not measured.">Collecting</dt><dd>{int(status.collectingCount!)}</dd></div>
          <div><dt title="Eligible sources with no recorded successful collection; may also be due or collecting.">Awaiting first success</dt><dd>{int(status.awaitingSuccessCount!)}</dd></div>
          <div className={styles.oldest}><dt>Oldest successful collection</dt><dd>{status.oldestSuccess ? <button type="button" aria-label={`Inspect oldest collection for ${status.oldestSuccess.sourceName}`} onClick={() => onOpenSource(status.oldestSuccess!.sourceKey)}>
            <time dateTime={status.oldestSuccess.at} title={sourceCollectionTimestamp(status.oldestSuccess.at)}>{formatRelativeTime(status.oldestSuccess.at)}</time><ArrowUpRight aria-hidden="true" />
          </button> : status.successDatesUnknown ? "Unknown" : "None recorded"}</dd></div>
        </dl>
        <p className={styles.scope}>{status.eligibleCount ? `${int(status.eligibleCount)} enabled, reviewed ${status.eligibleCount === 1 ? "source" : "sources"}. Collection times describe source refreshes, not individual event updates.` : "No sources are currently eligible for collection."}</p>
      </div>
    </div>}
    <SourceCollectionSchedule key={sourceKey} sources={roster?.items ?? null} sourceKey={sourceKey} readAt={roster?.read_at ?? ""}
      onRefresh={() => void snapshot.refresh()} onOpenSource={onOpenSource} />
  </section>;
}
