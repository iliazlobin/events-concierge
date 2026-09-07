import type { LocationScope } from "./types.ts";

export type LocationScopeKind = "Area" | "Neighborhood";

export interface CatalogLocationScope {
  value: LocationScope;
  label: string;
  kind: LocationScopeKind;
  aliases: string[];
  /** Used only as an empty-result map fallback. Server filtering owns final semantics. */
  bounds: [[number, number], [number, number]];
}

export const CATALOG_LOCATION_SCOPES: readonly CatalogLocationScope[] = Object.freeze([
  {
    value: "bay_area",
    label: "Bay Area",
    kind: "Area",
    aliases: ["sf bay area", "san francisco bay area", "nine county bay area"],
    bounds: [[-123.05, 36.93], [-121.18, 38.87]],
  },
  {
    value: "los_angeles_area",
    label: "Los Angeles area",
    kind: "Area",
    aliases: ["la area", "greater los angeles", "los angeles metro"],
    bounds: [[-118.75, 33.70], [-117.90, 34.34]],
  },
  {
    value: "manhattan",
    label: "Manhattan",
    kind: "Neighborhood",
    aliases: ["new york manhattan", "nyc manhattan"],
    bounds: [[-74.0479, 40.6829], [-73.9067, 40.879]],
  },
]);

export const CATALOG_LOCATION_SCOPE_VALUES = Object.freeze(
  CATALOG_LOCATION_SCOPES.map((scope) => scope.value),
);

export function locationScope(value: string): CatalogLocationScope | undefined {
  return CATALOG_LOCATION_SCOPES.find((scope) => scope.value === value);
}

export function formatLocationSelection(
  cities: string[],
  scopes: LocationScope[],
  formatCity: (value: string) => string,
): string {
  const labels = [
    ...scopes.flatMap((scope) => {
      const definition = locationScope(scope);
      return definition ? [definition.label] : [];
    }),
    ...cities.map(formatCity),
  ];
  if (!labels.length) return "Everywhere";
  if (labels.length === 1) return labels[0];
  return `${labels.length} places`;
}
