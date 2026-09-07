/**
 * Every public identity we hold for an entity, as links a reader can follow.
 *
 * The Overview used to say "A direct profile URL is on file." — a sentence about the existence of
 * a link, printed where the link itself would have fitted. It told a reader nothing they could
 * check, and it cost the same room a LinkedIn mark would have taken.
 *
 * What is on file today is still narrow: 1,342 LinkedIn URLs and about fifty websites across the
 * whole catalog. That was a *capture* limit, not a display one — the Luma adapter read
 * `linkedin_handle` and `website` off a host record and dropped `instagram_handle`,
 * `twitter_handle`, `tiktok_handle` and `youtube_handle` on the floor. The capture now carries all
 * of them into the enrichment plane, and this module recognises each network by host rather than by
 * which field it arrived in, so nothing here needs to change again as rows accumulate.
 */

export type IdentityNetwork =
  | "linkedin"
  | "x"
  | "instagram"
  | "youtube"
  | "tiktok"
  | "github"
  | "facebook"
  | "substack"
  | "luma"
  | "website";

export interface IdentityLink {
  network: IdentityNetwork;
  /** What the link is, for a tooltip and for assistive technology. */
  label: string;
  href: string;
}

/** Host suffixes to network. Order is irrelevant; the longest match is not needed, hosts are exact. */
const HOSTS: ReadonlyArray<readonly [string, IdentityNetwork, string]> = [
  ["linkedin.com", "linkedin", "LinkedIn"],
  ["x.com", "x", "X"],
  ["twitter.com", "x", "X"],
  ["instagram.com", "instagram", "Instagram"],
  ["youtube.com", "youtube", "YouTube"],
  ["youtu.be", "youtube", "YouTube"],
  ["tiktok.com", "tiktok", "TikTok"],
  ["github.com", "github", "GitHub"],
  ["facebook.com", "facebook", "Facebook"],
  ["substack.com", "substack", "Substack"],
  ["luma.com", "luma", "Luma"],
  ["lu.ma", "luma", "Luma"],
];

/** Rendering order: the identity that names a person first, the site they run last. */
const ORDER: readonly IdentityNetwork[] = [
  "linkedin",
  "x",
  "instagram",
  "youtube",
  "tiktok",
  "github",
  "facebook",
  "substack",
  "luma",
  "website",
];

/** `twitter.com` and `x.com` are one platform, so they must also be one dedupe key. */
function canonicalHost(hostname: string): string {
  const host = hostname.toLowerCase().replace(/\.+$/, "").replace(/^(www\.|m\.)/, "");
  return host === "twitter.com" ? "x.com" : host;
}

function classify(href: string): IdentityLink | null {
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return null;
  }
  /*
   * https only. Every URL that reaches here is stored under a validator that already refuses
   * anything else — `fn_normalize_social_profile_url_v1`, `fn_normalize_profile_url_v1` and the
   * external-source guard are all `^https://` — so an `http:` value is not a legacy row to be
   * tolerated, it is a value that did not come through those paths.
   */
  if (url.protocol !== "https:") return null;
  const host = canonicalHost(url.hostname);
  for (const [suffix, network, label] of HOSTS) {
    if (host === suffix || host.endsWith(`.${suffix}`)) {
      /*
       * The platform's own front page is not somebody's profile — but a subdomain is: a Substack
       * lives at `someone.substack.com` with no path at all, while `substack.com` with no path is
       * just Substack. So this needs the host to be the bare platform AND the path to be empty.
       */
      if (host === suffix && url.pathname.replace(/\/+$/, "") === "") {
        return { network: "website", label: url.hostname.replace(/^www\./, ""), href };
      }
      return { network, label, href };
    }
  }
  return { network: "website", label: url.hostname.replace(/^www\./, ""), href };
}

/**
 * Two links are the same identity when they point at the same place.
 *
 * Keyed on the normalised host `classify` already derived rather than on the raw href, because the
 * spellings that differ are exactly the ones the storage layer normalises away and the display
 * layer would otherwise draw twice: 27 entities today hold a `canonical_profile_url` and an
 * `official_website` differing only by `www.`, and a legacy `twitter.com` fact sits beside the
 * `x.com` source URL for the same profile. Two adjacent marks with the identical accessible name
 * and the identical destination are a defect a keyboard or screen-reader user pays for.
 */
function identityOf(link: IdentityLink): string {
  let url: URL;
  try {
    url = new URL(link.href);
  } catch {
    return link.href.replace(/\/+$/, "").toLowerCase();
  }
  return `${canonicalHost(url.hostname)}${url.pathname.replace(/\/+$/, "").toLowerCase()}`
    + `${url.search}${url.hash}`;
}

/**
 * Gather, classify, dedupe and order every URL held for one entity.
 *
 * `canonicalProfileUrl` leads because it is the URL the catalog keyed the identity on; the rest
 * are whatever public-data facts and provider records have accumulated behind it.
 */
export function identityLinks(
  canonicalProfileUrl: string | null | undefined,
  factUrls: readonly (string | null | undefined)[] = [],
  sourceUrls: readonly (string | null | undefined)[] = [],
): IdentityLink[] {
  const seen = new Set<string>();
  const links: IdentityLink[] = [];
  for (const href of [canonicalProfileUrl, ...factUrls, ...sourceUrls]) {
    if (!href) continue;
    const link = classify(href);
    if (!link) continue;
    const key = identityOf(link);
    if (seen.has(key)) continue;
    seen.add(key);
    links.push(link);
  }
  return links.sort((a, b) => ORDER.indexOf(a.network) - ORDER.indexOf(b.network));
}
