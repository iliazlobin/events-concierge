"use client";

import { getAdminSourceHealth } from "@/lib/admin-api";
import { CollectionTrends } from "./collection-trends";
import type { RunsViewProps } from "./runs-view";
import { Action, PageHead, kit } from "./console-kit";
import { useAdminSnapshot } from "./use-admin-snapshot";

const loadSources = (signal: AbortSignal) => getAdminSourceHealth(false, signal);

export function RunStatisticsView({ refreshVersion = 0, onOpenSource, onOpenCatalog }: {
  refreshVersion?: number;
  onOpenSource: RunsViewProps["onOpenSource"];
  onOpenCatalog: RunsViewProps["onOpenCatalog"];
}) {
  const sources = useAdminSnapshot(loadSources, refreshVersion);
  const snapshot = sources.failed || sources.authorizationDenied ? null : sources.data;

  return <div className={kit.page}>
    <PageHead eyebrow="Administration" title="Runs"
      sub="Collection volume, outcomes and output over time. Open an interval to inspect its runs." />
    {sources.failed ? <div className={kit.context} role="alert">
      <span>Source choices could not be loaded. All-source trends and saved source selections remain available.</span>
      <Action onClick={() => void sources.refresh()}>Retry sources</Action>
    </div> : null}
    <CollectionTrends sources={snapshot} refreshVersion={refreshVersion} onOpenSource={onOpenSource} onOpenCatalog={onOpenCatalog} />
  </div>;
}
