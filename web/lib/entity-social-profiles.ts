import type { CatalogEntityDetail, CatalogEntityExternalFact, CatalogEntityExternalSource } from "./types.ts";

export const SOCIAL_API_PROVIDERS = new Set(["x_public_api", "instagram_public_api"]);

export function socialProfileCards(detail: CatalogEntityDetail | null): {
  source: CatalogEntityExternalSource;
  description: string | null;
  followers: string | null;
  avatar: string | null;
}[] {
  return (detail?.external_sources ?? [])
    .filter((source) => SOCIAL_API_PROVIDERS.has(source.provider_key) && source.fetched_at)
    .map((source) => {
      const facts = (detail?.external_facts ?? []).filter((fact) => fact.provider_key === source.provider_key);
      const value = (key: CatalogEntityExternalFact["fact_key"]) => facts.find((fact) => fact.fact_key === key);
      const rawAvatar = value("avatar")?.value_url;
      let avatar: string | null = null;
      if (rawAvatar) {
        try {
          const url = new URL(rawAvatar);
          const hosts = source.provider_key === "x_public_api" ? ["pbs.twimg.com"] : ["cdninstagram.com", "fbcdn.net"];
          if (url.protocol === "https:" && !url.username && !url.password && !url.port && !url.hash
            && hosts.some((host) => url.hostname === host || url.hostname.endsWith(`.${host}`))) {
            avatar = rawAvatar;
          }
        } catch { /* Retain the neutral icon if the stored URL is invalid. */ }
      }
      const followers = value("followers")?.value;
      return { source, description: value("description")?.value ?? null,
        followers: followers && /^\d{1,13}$/.test(followers) ? Number(followers).toLocaleString("en-US") : null,
        avatar };
    });
}
