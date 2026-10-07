"use client";

import { useCallback, useEffect, useRef } from "react";

/** Narrow screens read details below the canvas, using the page's single scroll surface. */
export function useGraphDetailNavigation(
  selectedNodeId: string | null,
  onSelectNode: (nodeId: string | null) => void,
) {
  const stageRef = useRef<HTMLDivElement>(null);
  const previousSelection = useRef<string | null>(null);

  const scrollTo = useCallback((selector: string) => {
    if (!window.matchMedia("(max-width: 1100px)").matches) return;
    stageRef.current?.parentElement?.querySelector(selector)?.scrollIntoView({ block: "start", behavior: "instant" });
  }, []);

  useEffect(() => {
    const previous = previousSelection.current;
    previousSelection.current = selectedNodeId;
    if (!selectedNodeId) {
      if (previous) scrollTo(".graph-workspace-heading");
      return;
    }
    scrollTo(".entity-graph-inspector");
    const inspector = stageRef.current?.querySelector(".entity-graph-inspector");
    if (inspector?.getAttribute("aria-busy") !== "true") return;
    // An event's short loading card may not allow a full scroll until its facts arrive.
    const observer = new MutationObserver(() => {
      if (inspector.getAttribute("aria-busy") === "true") return;
      scrollTo(".entity-graph-inspector");
      observer.disconnect();
    });
    observer.observe(inspector, { attributes: true, attributeFilter: ["aria-busy"] });
    return () => observer.disconnect();
  }, [selectedNodeId, scrollTo]);

  const selectNode = useCallback((nodeId: string | null) => {
    onSelectNode(nodeId);
    // The focused entity already has no explicit selection, so its return button must also work.
    if (!nodeId) scrollTo(".graph-workspace-heading");
  }, [onSelectNode, scrollTo]);

  return { stageRef, selectNode };
}
