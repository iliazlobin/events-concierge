/**
 * Which node labels can rest visible, and how big a node is once the camera has had its say.
 *
 * Both answers are geometry, and both used to be guessed. Node size ignored the camera entirely —
 * positions were scaled and discs were not, so every zoom below 1 pulled the centres together
 * while the circles stayed full size and neighbours overlapped. And label visibility was decided
 * from a node *count*, which is a proxy for crowding rather than a measurement of it: it could
 * hide a dozen perfectly legible names on a sparse ring, and still let a packed overview print its
 * names on top of one another.
 *
 * A held-back label is never lost. It returns on hover, focus or selection, and the text view
 * lists every name in full.
 */

/** A node never shrinks below this, however far out the camera is pulled. */
export const MIN_SCREEN_RADIUS = 9;

/** Resting label metrics, in screen pixels; these track `.entity-graph-node__label` in the CSS. */
export const LABEL_MAX_WIDTH = 132;
export const LABEL_LINE_HEIGHT = 13;
/**
 * Deliberately a little wider than the font's true average.
 *
 * The estimate only has to be good enough to decide whether two labels collide, and over-stating
 * it hides a name that might just have fitted — which is the safe direction to be wrong in.
 */
export const LABEL_CHAR_WIDTH = 6.1;
export const LABEL_GAP = 5;
/** Breathing room between two resting labels before they read as one run of text. */
export const LABEL_CLEARANCE = 10;

export interface LabelBox {
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
}

export interface PlacedDisc {
  label: string;
  cx: number;
  cy: number;
  radius: number;
}

/**
 * A node's radius on screen.
 *
 * Scaling the disc with its position is what makes the clearance the layout computed and the
 * clearance the reader sees the same number.
 */
export function screenRadius(radius: number, k: number): number {
  return Math.max(MIN_SCREEN_RADIUS, radius * k);
}

/** The box a resting label would occupy, centred under its node. */
export function labelBox(label: string, x: number, y: number, radius: number): LabelBox {
  const width = Math.min(LABEL_MAX_WIDTH, label.length * LABEL_CHAR_WIDTH + 2);
  const top = y + radius + LABEL_GAP;
  return { minX: x - width / 2, maxX: x + width / 2, minY: top, maxY: top + LABEL_LINE_HEIGHT };
}

export function boxesOverlap(a: LabelBox, b: LabelBox): boolean {
  return (
    a.minX - LABEL_CLEARANCE < b.maxX
    && a.maxX + LABEL_CLEARANCE > b.minX
    && a.minY - LABEL_CLEARANCE < b.maxY
    && a.maxY + LABEL_CLEARANCE > b.minY
  );
}

/** True when a node's disc reaches into a label's box. */
export function discOverlapsBox(disc: PlacedDisc, box: LabelBox): boolean {
  const nearestX = Math.max(box.minX, Math.min(disc.cx, box.maxX));
  const nearestY = Math.max(box.minY, Math.min(disc.cy, box.maxY));
  return Math.hypot(disc.cx - nearestX, disc.cy - nearestY) < disc.radius + LABEL_CLEARANCE / 2;
}

/**
 * The indices whose labels are legible where they landed.
 *
 * Greedy, in the order given — the caller passes inner rings first, then clockwise — so the names
 * that survive are stable as the camera moves and never depend on paint order. A label has to
 * clear two different things: every label already kept, and every node's disc. The second matters
 * on its own, because a name that reads cleanly against its neighbours' names can still be printed
 * straight across the next row's circles, which is the more obviously broken of the two.
 */
export function chooseRestingLabels(placed: readonly PlacedDisc[]): boolean[] {
  const kept: LabelBox[] = [];
  return placed.map((entry) => {
    const box = labelBox(entry.label, entry.cx, entry.cy, entry.radius);
    const legible = !kept.some((other) => boxesOverlap(box, other))
      && !placed.some((other) => other !== entry && discOverlapsBox(other, box));
    if (legible) kept.push(box);
    return legible;
  });
}
