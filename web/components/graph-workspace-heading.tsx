"use client";

import { ChevronRight } from "lucide-react";
import type { ReactNode } from "react";

export function GraphWorkspaceHeading({ current, onRoot, summary, children }: {
  current?: string;
  onRoot?: () => void;
  summary?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <header className="graph-workspace-heading">
      <div>
        {current ? (
          <nav className="graph-workspace-path" aria-label="Graph navigation">
            <button type="button" onClick={onRoot}>Graph</button>
            <ChevronRight aria-hidden="true" />
            <h1 aria-current="page">{current}</h1>
          </nav>
        ) : <h1>Graph</h1>}
        {summary ? <p className="graph-workspace-summary">{summary}</p> : null}
      </div>
      {children}
    </header>
  );
}
