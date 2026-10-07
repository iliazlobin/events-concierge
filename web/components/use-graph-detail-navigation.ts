"use client";

import { useCallback, useEffect, useRef } from "react";

/** Narrow screens read details below the canvas, using the page's single scroll surface. */
export function useGraphDetailNavigation(
  selectedNodeId: string | null,
  onSelectNode: (nodeId: string | null) => void,
) {
  const stageRef = useRef<HTMLDivElement>(null);
  const previousSelection = useRef<string | null>(null);
  const scrollTimer = useRef<number | null>(null);
  const scrollObserver = useRef<MutationObserver | null>(null);

  const cancelScroll = useCallback(() => {
    if (scrollTimer.current !== null) window.clearTimeout(scrollTimer.current);
    scrollTimer.current = null;
    scrollObserver.current?.disconnect();
    scrollObserver.current = null;
  }, []);

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
    // Keep the canvas in place while a second tap can focus an entity or recenter a node.
    scrollTimer.current = window.setTimeout(() => {
      scrollTimer.current = null;
      scrollTo(".entity-graph-inspector");
      const inspector = stageRef.current?.querySelector(".entity-graph-inspector");
      if (inspector?.getAttribute("aria-busy") !== "true") return;
      // An event's short loading card may not allow a full scroll until its facts arrive.
      const observer = new MutationObserver(() => {
        if (inspector.getAttribute("aria-busy") === "true") return;
        scrollTo(".entity-graph-inspector");
        observer.disconnect();
        scrollObserver.current = null;
      });
      scrollObserver.current = observer;
      observer.observe(inspector, { attributes: true, attributeFilter: ["aria-busy"] });
    }, 500);
    return cancelScroll;
  }, [selectedNodeId, scrollTo, cancelScroll]);

  const selectNode = useCallback((nodeId: string | null) => {
    if (!nodeId) cancelScroll();
    onSelectNode(nodeId);
    // The focused entity already has no explicit selection, so its return button must also work.
    if (!nodeId) scrollTo(".graph-workspace-heading");
  }, [onSelectNode, scrollTo, cancelScroll]);

  return { stageRef, selectNode, cancelScroll };
}
