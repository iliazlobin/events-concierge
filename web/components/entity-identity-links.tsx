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

/**
 * A neutral glyph vocabulary, not a brand one.
 *
 * An earlier draft of this file inlined the official LinkedIn, X, Instagram, YouTube, TikTok,
 * GitHub, Facebook and Substack marks as raw path data and painted each in its brand colour on
 * hover. Two things are wrong with that and both are structural. It ships trademarked assets into
 * the bundle with no licence. And a platform's own mark, in the platform's own colour, sitting
 * immediately above the sentence "Holding it is not a verification that it belongs to this person"
 * reads as exactly the verification badge that sentence denies — the record holds a URL a source
 * asserted, and nothing was fetched, confirmed, or endorsed.
 *
 * These glyphs name the *kind* of page a link is instead, which is all the record actually knows,
 * and they are the same vocabulary `EntitySourceGlyph` uses on the Profile tab so one entity does
 * not get two visual languages. The network name is rendered as text beside the glyph rather than
 * hidden behind it: a neutral glyph is not self-identifying the way a brand mark is, so the name has
 * to be visible, not only announced to assistive technology.
 */
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

/**
 * The identity row.
 *
 * Each link states what it is and where it goes: the glyph places it at a glance, the label names
 * the network, and the full URL stays on the hover title and on the Profile & sources tab for a
 * reader who wants to read it character by character.
 */
export function EntityIdentityLinks({ links }: { links: readonly IdentityLink[] }) {
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
          >
            {GLYPHS[link.network] ?? GLYPHS.website}
            <span>{link.label}</span>
          </a>
        </li>
      ))}
    </ul>
  );
}
