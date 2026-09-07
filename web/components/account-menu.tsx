"use client";

import {
  Bookmark,
  KeyRound,
  LogOut,
  Settings2,
  Sparkles,
  SlidersHorizontal,
  ShieldAlert,
  ShieldCheck,
  Wrench,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import type { Me, UiConfig } from "@/lib/types";

interface AccountMenuProps {
  me: Me | null;
  config: UiConfig | null;
  signingOut: boolean;
  onSignOut: () => void;
}

interface MenuLink {
  href: string;
  label: string;
  hint: string;
  icon: typeof Settings2;
}

const LINKS: MenuLink[] = [
  { href: "/settings", label: "Profile", hint: "Name, photo, zone", icon: Settings2 },
  { href: "/settings/taste", label: "Interests", hint: "What it looks for", icon: Sparkles },
  { href: "/settings/saved-filters", label: "Saved filters", hint: "Selections you kept", icon: Bookmark },
  { href: "/settings/activity", label: "Activity", hint: "Asks, registrations", icon: SlidersHorizontal },
  { href: "/settings/api-keys", label: "API keys", hint: "Programmatic access", icon: KeyRound },
  { href: "/settings/security", label: "Security", hint: "Password, two-factor", icon: ShieldCheck },
  { href: "/settings/account", label: "Account and data", hint: "Sign-in, deletion", icon: ShieldAlert },
];

/**
 * The header account menu.
 *
 * This is a *command* menu, not one of the app's value pickers, so it carries `menu`/`menuitem` and
 * roving focus rather than the `listbox`/`option` contract the comboboxes use. Dismissal is
 * deliberately the same wrapper-blur mechanism they use: no document listener, no portal, and no
 * focus trap -- a menu button must let Tab move focus out rather than cycling it.
 *
 * Navigation entries are real anchors so a middle-click or Cmd-click opens a tab, which is how the
 * administration affordance this menu absorbs has always behaved.
 */
export function AccountMenu({ me, config, signingOut, onSignOut }: AccountMenuProps) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const itemRefs = useRef<Array<HTMLAnchorElement | HTMLButtonElement | null>>([]);

  const displayName = me?.profile?.display_name?.trim() || null;
  const email = me?.notify_email ?? null;
  const initial = (displayName ?? email ?? "").trim().charAt(0).toUpperCase();
  const avatarUrl = me?.profile?.avatar_url ?? null;
  // Operator affordance, shown only to accounts holding a granted role. This is
  // presentation: the administration API enforces its own access separately.
  const showAdmin = Boolean(me?.is_admin);

  // The rendered order the roving index walks: links, the optional admin hop, then sign out.
  const itemCount = LINKS.length + (showAdmin ? 1 : 0) + 1;

  const close = useCallback((restoreFocus: boolean) => {
    setOpen(false);
    if (restoreFocus) {
      // The trigger may still be mid-blur; defer so the focus call is not swallowed.
      window.requestAnimationFrame(() => triggerRef.current?.focus());
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    const target = itemRefs.current[activeIndex];
    target?.focus();
  }, [open, activeIndex]);

  const handleBlur = useCallback(() => {
    // Focus moves between children before it settles; let it land before judging.
    window.setTimeout(() => {
      const wrapper = wrapperRef.current;
      if (wrapper && !wrapper.contains(document.activeElement)) setOpen(false);
    }, 0);
  }, []);

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close(true);
        return;
      }
      if (!open) return;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setActiveIndex((current) => (current + 1) % itemCount);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        setActiveIndex((current) => (current - 1 + itemCount) % itemCount);
      } else if (event.key === "Home") {
        event.preventDefault();
        setActiveIndex(0);
      } else if (event.key === "End") {
        event.preventDefault();
        setActiveIndex(itemCount - 1);
      }
    },
    [close, itemCount, open],
  );

  const openWith = useCallback((index: number) => {
    setActiveIndex(index);
    setOpen(true);
  }, []);

  const handleTriggerKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLButtonElement>) => {
      if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openWith(0);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        openWith(itemCount - 1);
      }
    },
    [itemCount, openWith],
  );

  let cursor = -1;
  const nextIndex = (): number => {
    cursor += 1;
    return cursor;
  };

  return (
    <div className="account-menu" ref={wrapperRef} onBlur={handleBlur} onKeyDown={handleKeyDown}>
      <button
        type="button"
        ref={triggerRef}
        className="profile-dot account-menu__trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Account menu${email ? ` for ${email}` : ""}`}
        onKeyDown={handleTriggerKeyDown}
        onClick={(event) => {
          // Safari does not focus a button on click, and the blur contract needs the wrapper focused.
          event.currentTarget.focus();
          if (open) close(false);
          else openWith(0);
        }}
      >
        {avatarUrl ? (
          <img className="account-menu__avatar" src={avatarUrl} alt="" width={30} height={30} />
        ) : (
          initial || <span aria-hidden="true">&nbsp;</span>
        )}
      </button>

      {open ? (
        <div className="account-menu__panel" role="menu" aria-label="Account">
          <div className="account-menu__identity">
            <p className="account-menu__name">{displayName ?? "Your account"}</p>
            {email ? <p className="account-menu__email">{email}</p> : null}
          </div>

          <div className="account-menu__group">
            {LINKS.map((link) => {
              const index = nextIndex();
              const Icon = link.icon;
              return (
                <a
                  key={link.href}
                  role="menuitem"
                  href={link.href}
                  className="account-menu__item"
                  tabIndex={activeIndex === index ? 0 : -1}
                  ref={(node) => {
                    itemRefs.current[index] = node;
                  }}
                  onMouseDown={(event) => event.preventDefault()}
                  onMouseEnter={() => setActiveIndex(index)}
                >
                  <Icon className="account-menu__icon" aria-hidden="true" />
                  <span className="account-menu__label">{link.label}</span>
                  <span className="account-menu__hint">{link.hint}</span>
                </a>
              );
            })}
          </div>

          {showAdmin ? (
            <div className="account-menu__group account-menu__group--bordered">
              {(() => {
                const index = nextIndex();
                return (
                  <a
                    role="menuitem"
                    href="/admin"
                    className="account-menu__item"
                    tabIndex={activeIndex === index ? 0 : -1}
                    ref={(node) => {
                      itemRefs.current[index] = node;
                    }}
                    onMouseDown={(event) => event.preventDefault()}
                    onMouseEnter={() => setActiveIndex(index)}
                  >
                    <Wrench className="account-menu__icon" aria-hidden="true" />
                    <span className="account-menu__label">Open ingestion administration</span>
                  </a>
                );
              })()}
            </div>
          ) : null}

          <div className="account-menu__group account-menu__group--bordered">
            {(() => {
              const index = nextIndex();
              return (
                <button
                  type="button"
                  role="menuitem"
                  className="account-menu__item account-menu__item--danger"
                  tabIndex={activeIndex === index ? 0 : -1}
                  disabled={signingOut}
                  ref={(node) => {
                    itemRefs.current[index] = node;
                  }}
                  onMouseDown={(event) => event.preventDefault()}
                  onMouseEnter={() => setActiveIndex(index)}
                  onClick={() => {
                    setOpen(false);
                    onSignOut();
                  }}
                >
                  <LogOut className="account-menu__icon" aria-hidden="true" />
                  <span className="account-menu__label">
                    {signingOut
                      ? "Signing out…"
                      : config?.auth_mode === "deployment_session"
                        ? "Sign out"
                        : "Leave this session"}
                  </span>
                </button>
              );
            })()}
          </div>
        </div>
      ) : null}
    </div>
  );
}
