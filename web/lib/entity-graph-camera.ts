import type { EntityGraphScene } from "./entity-graph.ts";
import { NODE_MAX_RADIUS } from "./entity-graph-layout.ts";
import { MIN_SCREEN_RADIUS } from "./entity-graph-labels.ts";

export const MAX_GRAPH_ZOOM = 2.5;
export const GRAPH_CONTROL_CLEARANCE = 80;
const FIT_PADDING = NODE_MAX_RADIUS + 26;
const SCREEN_PADDING = MIN_SCREEN_RADIUS + 4;

export interface GraphCamera {
  tx: number;
  ty: number;
  k: number;
}

/** Fit every node above the zoom rail, including the minimum rendered disc radius. */
export function fitGraphCamera(
  bounds: EntityGraphScene["bounds"],
  width: number,
  height: number,
): GraphCamera {
  const spanX = Math.max(bounds.maxX - bounds.minX, 1) + FIT_PADDING * 2;
  const spanY = Math.max(bounds.maxY - bounds.minY, 1) + FIT_PADDING * 2;
  const availableHeight = Math.max(1, height - GRAPH_CONTROL_CLEARANCE);
  const k = Math.min(
    MAX_GRAPH_ZOOM,
    Math.max(1, width - SCREEN_PADDING * 2) / spanX,
    Math.max(1, availableHeight - SCREEN_PADDING * 2) / spanY,
  );
  const centreX = (bounds.minX + bounds.maxX) / 2;
  const centreY = (bounds.minY + bounds.maxY) / 2;
  return { k, tx: width / 2 - centreX * k, ty: availableHeight / 2 - centreY * k };
}

/** Zoom out must be able to reach Fit even on a small screen or a populated graph. */
export function minimumGraphZoom(bounds: EntityGraphScene["bounds"], width: number, height: number): number {
  return Math.min(0.5, fitGraphCamera(bounds, width, height).k);
}
