import {
  AtSign,
  Camera,
  Code2,
  Globe2,
  IdCard,
  Music,
  Ticket,
  Video,
} from "lucide-react";
import type { ReactNode } from "react";

import type { IdentityLink, IdentityNetwork } from "@/lib/entity-identity-links";

/** Neutral platform glyphs; URLs and optional handles come from stored source links. */
const GLYPHS: Record<IdentityNetwork, ReactNode> = {
  linkedin: <IdCard aria-hidden="true" />,
  x: <AtSign aria-hidden="true" />,
  instagram: <Camera aria-hidden="true" />,
  youtube: <Video aria-hidden="true" />,
  tiktok: <Music aria-hidden="true" />,
  github: <Code2 aria-hidden="true" />,
  facebook: <AtSign aria-hidden="true" />,
  substack: <IdCard aria-hidden="true" />,
  luma: <Ticket aria-hidden="true" />,
  website: <Globe2 aria-hidden="true" />,
};

export function EntityIdentityLinks({ links, showHandles = false }: {
  links: readonly IdentityLink[];
  showHandles?: boolean;
}) {
  if (!links.length) return null;
  return (
    <ul className="entity-identity-links">
      {links.map((link) => (
        <li key={link.href}>
          <a
            href={link.href}
            target="_blank"
            rel="noopener noreferrer"
            title={`${link.label} — ${link.href}`}
            data-network={link.network}
            aria-label={`${link.label}${showHandles && link.handle ? ` @${link.handle}` : ""}`}
          >
            {GLYPHS[link.network] ?? GLYPHS.website}
            <span>{link.label}</span>
            {showHandles && link.handle ? <small>@{link.handle}</small> : null}
          </a>
        </li>
      ))}
    </ul>
  );
}
