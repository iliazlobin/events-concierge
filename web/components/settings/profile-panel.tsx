"use client";

import { Check, LoaderCircle, Trash2, Upload } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";

import { useSession } from "@/components/session-provider";
import { SettingsActions, SettingsSaveButton } from "@/components/settings/settings-actions";
import { SettingsHelp } from "@/components/settings/settings-help";
import { useAvatarSource } from "@/components/use-avatar-source";
import { deleteAvatar, readableError, updateProfile, uploadAvatar } from "@/lib/api";
import { AvatarPrepareError, prepareAvatar } from "@/lib/avatar-image";
import { supportedTimeZones, validateDisplayName } from "@/lib/profile-form";
import { releaseProfile } from "@/lib/release-profile";

/**
 * The edit-profile form.
 *
 * The save is pessimistic: the write carries a caller-minted revision and conflicts on a stale one,
 * so an optimistic paint would have to be rolled back, refetched and then explained. The name also
 * feeds the header avatar, and a name that appears and then reverts reads worse than a brief spinner.
 */
export function ProfilePanel() {
  const { me, config, tenantId, applyMe } = useSession();
  const fullRelease = releaseProfile(config) === "full";
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
  const avatarSource = useAvatarSource(avatarUrl, tenantId);

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
        <Link className="settings-guide-link" href="/docs/account#profile">Guide <span aria-hidden="true">→</span></Link>
      </header>

      <section className="settings-card">
        <div className="settings-title-row">
          <h2 className="settings-card__title">Photo</h2>
          <SettingsHelp label="Photo">PNG, JPEG or WebP. Cropped to a 256-pixel square with camera metadata removed.</SettingsHelp>
        </div>
        <div className="profile-photo">
          {avatarSource ? (
            <img className="profile-photo__image" src={avatarSource} alt="Your profile photo" />
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
            ) : null}
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
          <div className="settings-field-label">
            <label htmlFor="display-name">Display name</label>
            <SettingsHelp label="Display name">Optional. Up to 64 characters.</SettingsHelp>
          </div>
          <input
            id="display-name"
            type="text"
            maxLength={64}
            autoComplete="name"
            value={displayName}
            placeholder="Name"
            aria-invalid={nameError ? true : undefined}
            aria-describedby={nameError ? "display-name-error" : undefined}
            onChange={(event) => {
              setDisplayName(event.target.value);
              if (nameError) setNameError(validateDisplayName(event.target.value));
            }}
            onBlur={() => setNameError(validateDisplayName(displayName))}
          />
          {nameError ? (
            <p className="settings-error" id="display-name-error" role="alert">
              {nameError}
            </p>
          ) : null}
        </div>

        <dl className="settings-facts">
          <div>
            <dt className="settings-field-label">Email <SettingsHelp label="Email">From your sign-in identity. It cannot be changed here.</SettingsHelp></dt>
            <dd>{me?.notify_email ?? "—"}</dd>
          </div>
          {fullRelease && me?.relay_inbox ? (
            <div>
              <dt className="settings-field-label">Relay inbox <SettingsHelp label="Relay inbox">Receives sign-in codes from event providers. It is inbound only and cannot be changed.</SettingsHelp></dt>
              <dd className="settings-mono">{me.relay_inbox}</dd>
            </div>
          ) : null}
        </dl>

        <div className="settings-field">
          <div className="settings-field-label">
            <label htmlFor="time-zone">Time zone</label>
            <SettingsHelp label="Time zone">{fullRelease ? "Used to interpret time requests such as “this evening”." : "Saved with your profile. Event times use this device’s time zone."}</SettingsHelp>
          </div>
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
        </div>
      </section>

      {saveError ? (
        <p className="settings-error" role="alert">
          {saveError}
        </p>
      ) : null}

      <SettingsActions
        status={
          saved ? (
            <>
              <Check aria-hidden="true" /> Profile saved.
            </>
          ) : dirty ? (
            "Unsaved changes."
          ) : (
            ""
          )
        }
      >
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
        <SettingsSaveButton type="submit" saving={saving} disabled={!dirty} />
      </SettingsActions>
    </form>
  );
}
