"use client";

import { Check, LoaderCircle, Trash2, Upload } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { useSession } from "@/components/session-provider";
import { deleteAvatar, readableError, updateProfile, uploadAvatar } from "@/lib/api";
import { AvatarPrepareError, prepareAvatar } from "@/lib/avatar-image";
import { supportedTimeZones, validateDisplayName } from "@/lib/profile-form";

/**
 * The edit-profile form.
 *
 * The save is pessimistic: the write carries a caller-minted revision and conflicts on a stale one,
 * so an optimistic paint would have to be rolled back, refetched and then explained. The name also
 * feeds the header avatar, and a name that appears and then reverts reads worse than a brief spinner.
 */
export function ProfilePanel() {
  const { me, tenantId, applyMe } = useSession();
  const profile = me?.profile ?? null;

  const baseline = useMemo(
    () => ({
      displayName: profile?.display_name ?? "",
      timeZone: profile?.time_zone ?? "",
    }),
    [profile?.display_name, profile?.time_zone],
  );

  const [displayName, setDisplayName] = useState(baseline.displayName);
  const [timeZone, setTimeZone] = useState(baseline.timeZone);
  const [nameError, setNameError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);
  const [avatarBusy, setAvatarBusy] = useState(false);
  const [avatarError, setAvatarError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const avatarUrl = profile?.avatar_url ?? null;

  // Re-seed when the server hands back a newer revision, so a conflict resolves into fresh truth.
  useEffect(() => {
    setDisplayName(baseline.displayName);
    setTimeZone(baseline.timeZone);
  }, [baseline]);

  const dirty = displayName !== baseline.displayName || timeZone !== baseline.timeZone;
  const zones = useMemo(supportedTimeZones, []);

  useEffect(() => {
    if (!dirty) return undefined;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const handleAvatarChange = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    // Clear immediately so choosing the same file twice still fires a change event.
    event.target.value = "";
    if (!file) return;

    setAvatarBusy(true);
    setAvatarError(null);
    try {
      const prepared = await prepareAvatar(file);
      const uploaded = await uploadAvatar(prepared, tenantId);
      if (me) {
        applyMe({
          ...me,
          profile: {
            display_name: profile?.display_name ?? null,
            time_zone: profile?.time_zone ?? null,
            revision: profile?.revision ?? 0,
            updated_at: profile?.updated_at ?? null,
            avatar_url: uploaded.avatar_url,
          },
        });
      }
    } catch (error) {
      setAvatarError(
        error instanceof AvatarPrepareError
          ? error.message
          : readableError(error, "That photo could not be uploaded."),
      );
    } finally {
      setAvatarBusy(false);
    }
  };

  const handleAvatarRemove = async () => {
    setAvatarBusy(true);
    setAvatarError(null);
    try {
      await deleteAvatar(tenantId);
      if (me) {
        applyMe({
          ...me,
          profile: {
            display_name: profile?.display_name ?? null,
            time_zone: profile?.time_zone ?? null,
            revision: profile?.revision ?? 0,
            updated_at: profile?.updated_at ?? null,
            avatar_url: null,
          },
        });
      }
    } catch (error) {
      setAvatarError(readableError(error, "That photo could not be removed."));
    } finally {
      setAvatarBusy(false);
    }
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    const problem = validateDisplayName(displayName);
    setNameError(problem);
    if (problem) return;

    setSaving(true);
    setSaveError(null);
    setSaved(false);
    try {
      const next = await updateProfile(
        {
          display_name: displayName.trim() === "" ? null : displayName.trim(),
          time_zone: timeZone === "" ? null : timeZone,
          revision: (profile?.revision ?? 0) + 1,
        },
        tenantId,
      );
      if (me) applyMe({ ...me, profile: next });
      setSaved(true);
      window.setTimeout(() => setSaved(false), 4000);
    } catch (error) {
      setSaveError(readableError(error, "Your changes were not saved."));
    } finally {
      setSaving(false);
    }
  };

  const initial = (displayName || me?.notify_email || "").trim().charAt(0).toUpperCase();

  return (
    <form className="settings-panel" onSubmit={handleSubmit}>
      <header className="settings-panel__head">
        <h1>Profile</h1>
        <p>How the concierge addresses you.</p>
      </header>

      <section className="settings-card">
        <h2 className="settings-card__title">Photo</h2>
        <div className="profile-photo">
          {avatarUrl ? (
            <img className="profile-photo__image" src={avatarUrl} alt="Your profile photo" />
          ) : (
            <span className="profile-photo__tile" aria-hidden="true">
              {initial}
            </span>
          )}
          <div className="profile-photo__controls">
            <div className="profile-photo__actions">
              <button
                type="button"
                className="button button-quiet"
                disabled={avatarBusy}
                onClick={() => fileRef.current?.click()}
              >
                {avatarBusy ? (
                  <LoaderCircle className="spin" aria-hidden="true" />
                ) : (
                  <Upload aria-hidden="true" />
                )}
                {avatarUrl ? "Replace photo" : "Upload photo"}
              </button>
              {avatarUrl ? (
                <button
                  type="button"
                  className="button button-quiet-danger"
                  disabled={avatarBusy}
                  onClick={handleAvatarRemove}
                >
                  <Trash2 aria-hidden="true" />
                  Remove
                </button>
              ) : null}
            </div>
            {avatarError ? (
              <p className="settings-error" role="alert">
                {avatarError}
              </p>
            ) : (
              <p className="settings-hint">
                PNG, JPEG, or WebP. Cropped to a {256}px square and stripped of camera metadata.
              </p>
            )}
          </div>
          <input
            ref={fileRef}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            className="visually-hidden"
            tabIndex={-1}
            aria-hidden="true"
            onChange={handleAvatarChange}
          />
        </div>
      </section>

      <section className="settings-card">
        <div className="settings-field">
          <label htmlFor="display-name">Display name</label>
          <input
            id="display-name"
            type="text"
            maxLength={64}
            autoComplete="name"
            value={displayName}
            placeholder="How should the concierge address you?"
            aria-invalid={nameError ? true : undefined}
            aria-describedby={nameError ? "display-name-error" : "display-name-hint"}
            onChange={(event) => setDisplayName(event.target.value)}
            onBlur={() => setNameError(validateDisplayName(displayName))}
          />
          {nameError ? (
            <p className="settings-error" id="display-name-error" role="alert">
              {nameError}
            </p>
          ) : (
            <p className="settings-hint" id="display-name-hint">
              Up to 64 characters.
            </p>
          )}
        </div>

        <div className="settings-readonly">
          <dt>Notification email</dt>
          <dd>
            {me?.notify_email ?? "—"}
            <span className="settings-fixed">fixed</span>
          </dd>
          <p className="settings-hint">
            This is where the concierge sends confirmations and anything that needs you. It is bound
            to your sign-in identity and cannot be changed here yet.
          </p>
        </div>

        {me?.relay_inbox ? (
          <div className="settings-readonly">
            <dt>Relay inbox</dt>
            <dd className="settings-mono">{me.relay_inbox}</dd>
            <p className="settings-hint">
              Where sign-in codes from event sites are received on your behalf. Inbound only — it
              never sends, and it cannot be changed.
            </p>
          </div>
        ) : null}

        <div className="settings-field">
          <label htmlFor="time-zone">Time zone</label>
          <select
            id="time-zone"
            value={timeZone}
            onChange={(event) => setTimeZone(event.target.value)}
          >
            <option value="">Use this device&rsquo;s zone</option>
            {zones.map((zone) => (
              <option key={zone} value={zone}>
                {zone}
              </option>
            ))}
          </select>
          <p className="settings-hint">Used when the concierge reasons about &ldquo;this evening&rdquo;.</p>
        </div>
      </section>

      {saveError ? (
        <p className="settings-error" role="alert">
          {saveError}
        </p>
      ) : null}

      <footer className="settings-actions">
        <span className="settings-actions__state" role="status">
          {saved ? (
            <>
              <Check aria-hidden="true" /> Profile saved.
            </>
          ) : dirty ? (
            "Unsaved changes."
          ) : (
            ""
          )}
        </span>
        <button
          type="button"
          className="button button-quiet"
          disabled={!dirty || saving}
          onClick={() => {
            setDisplayName(baseline.displayName);
            setTimeZone(baseline.timeZone);
            setNameError(null);
            setSaveError(null);
          }}
        >
          Cancel
        </button>
        <button type="submit" className="button button-primary" disabled={!dirty || saving}>
          {saving ? <LoaderCircle className="spin" aria-hidden="true" /> : null}
          Save changes
        </button>
      </footer>
    </form>
  );
}
