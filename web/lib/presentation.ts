const CITY_NAMES: Record<string, string> = {
  alameda: "Alameda",
  berkeley: "Berkeley",
  brentwood: "Brentwood",
  burlingame: "Burlingame",
  campbell: "Campbell",
  concord: "Concord",
  crockett: "Crockett",
  cupertino: "Cupertino",
  dalycity: "Daly City",
  fremont: "Fremont",
  gilroy: "Gilroy",
  halfmoonbay: "Half Moon Bay",
  hayward: "Hayward",
  lafayette: "Lafayette",
  livermore: "Livermore",
  losangeles: "Los Angeles",
  losaltos: "Los Altos",
  losgatos: "Los Gatos",
  martinez: "Martinez",
  menlopark: "Menlo Park",
  millbrae: "Millbrae",
  milpitas: "Milpitas",
  mountainview: "Mountain View",
  newyork: "New York",
  oakland: "Oakland",
  orinda: "Orinda",
  pacifica: "Pacifica",
  paloalto: "Palo Alto",
  pittsburg: "Pittsburg",
  pleasanton: "Pleasanton",
  pleasanthill: "Pleasant Hill",
  redwoodcity: "Redwood City",
  richmond: "Richmond",
  sanbruno: "San Bruno",
  sanfrancisco: "San Francisco",
  sanjose: "San José",
  sanleandro: "San Leandro",
  sanmateo: "San Mateo",
  sanramon: "San Ramon",
  santamonica: "Santa Monica",
  santaclara: "Santa Clara",
  southsanfrancisco: "South San Francisco",
  sunnyvale: "Sunnyvale",
  unioncity: "Union City",
  walnutcreek: "Walnut Creek",
};

export const DEFAULT_CATALOG_CITY = "sanfrancisco";
export const CATALOG_CITY_VALUES = Object.freeze(Object.keys(CITY_NAMES));

function comparisonKey(value: string): string {
  return value.normalize("NFKD").replace(/[^a-z0-9]/gi, "").toLowerCase();
}

export function formatCity(value: string | null | undefined): string {
  const trimmed = value?.trim();
  if (!trimmed) return "";
  const key = comparisonKey(trimmed);
  const known = CITY_NAMES[key];
  if (known) return known;
  return trimmed
    .split(/[\s_-]+/)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1).toLowerCase())
    .join(" ");
}

export function formatLocation(
  venue: string | null | undefined,
  city: string | null | undefined,
): string {
  const venueCopy = venue?.trim() ?? "";
  const cityCopy = formatCity(city);
  if (venueCopy && cityCopy && comparisonKey(venueCopy) === comparisonKey(cityCopy)) {
    return cityCopy;
  }
  return [venueCopy, cityCopy].filter(Boolean).join(" · ") || "Location TBA";
}
