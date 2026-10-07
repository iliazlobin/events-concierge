"use client";

import { useEffect, useMemo } from "react";
import { EntityGraphCanvas } from "@/components/entity-graph-canvas";
import { EntityInspector } from "@/components/entity-inspector";
import { useGraphDetailNavigation } from "@/components/use-graph-detail-navigation";
import { deriveEntityGraphDetails } from "@/lib/entity-graph";
import type { CatalogEntityGraphNode, EntityGraphSceneModel, EntityGraphScene } from "@/lib/entity-graph";
import type { EventEntityReference } from "@/lib/types";

/** The same selection and reading panel for catalog and entity neighborhoods. */
export function GraphWorkspace({
  scene, model, scope = "entity", tenantId, canRefresh = false,
  selectedNodeId, hoveredNodeId, canvasSelectedNodeId = selectedNodeId,
  canvasHoveredNodeId = hoveredNodeId, eventSessions, measureColumn,
  onSelectNode, onHoverNode, onFocusNode, onFocusEntity, onEntitySelect, onTopicSelect,
}: {
  scene: EntityGraphScene;
  model: EntityGraphSceneModel;
  scope?: "catalog" | "entity";
  tenantId: string | null;
  canRefresh?: boolean;
  selectedNodeId: string | null;
  hoveredNodeId: string | null;
  canvasSelectedNodeId?: string | null;
  canvasHoveredNodeId?: string | null;
  eventSessions?: CatalogEntityGraphNode[];
  measureColumn: (node: HTMLDivElement | null) => void;
  onSelectNode: (nodeId: string | null) => void;
  onHoverNode: (nodeId: string | null) => void;
  onFocusNode: (nodeId: string) => void;
  onFocusEntity: (entityId: string) => void;
  onEntitySelect: (reference: EventEntityReference) => void;
  onTopicSelect: (topic: string) => void;
}) {
  const detailModel = useMemo(() => deriveEntityGraphDetails(model), [model]);
  const inspectedNode = selectedNodeId ? model.byId.get(selectedNodeId) : scope === "entity" ? model.ego : null;
  const { stageRef, selectNode, cancelScroll } = useGraphDetailNavigation(selectedNodeId, onSelectNode);
  useEffect(() => {
    if (scope !== "catalog") return;
    const close = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.target instanceof Element && event.target.closest("input, textarea, select, [contenteditable]")) return;
      onSelectNode(null);
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [scope, onSelectNode]);
  return (
    <div className="entity-graph-stage" ref={stageRef} data-selection={inspectedNode?.node_kind ?? "none"}
      onDoubleClickCapture={cancelScroll}
      onKeyDownCapture={(event) => { if (event.key === "Enter" && event.shiftKey) cancelScroll(); }}>
      <div className="entity-graph-column" ref={measureColumn}>
        <EntityGraphCanvas scene={scene} selectedNodeId={canvasSelectedNodeId}
          hoveredNodeId={canvasHoveredNodeId} onSelect={selectNode} onFocus={onFocusNode} />
      </div>
      {inspectedNode ? <EntityInspector scope={scope} tenantId={tenantId} canRefresh={canRefresh}
        model={model} detailModel={detailModel} eventSessions={eventSessions}
        selectedNodeId={selectedNodeId} onSelectNode={selectNode} onHoverNode={onHoverNode}
        onFocusEntity={onFocusEntity} onEntitySelect={onEntitySelect} onTopicSelect={onTopicSelect} /> : null}
    </div>
  );
}
