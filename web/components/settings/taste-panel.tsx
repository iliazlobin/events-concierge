"use client";

import { Check, LoaderCircle, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { useSession } from "@/components/session-provider";
import { readableError, updatePreferences } from "@/lib/api";
import { releaseProfile } from "@/lib/release-profile";

const SUGGESTED = [
  { value: "live music", label: "Live music" },
  { value: "art", label: "Art & design" },
  { value: "technology", label: "Tech & ideas" },
  { value: "comedy", label: "Comedy" },
  { value: "outdoors", label: "Outdoors" },
  { value: "food", label: "Food" },
  { value: "community", label: "Community" },
  { value: "wellness", label: "Wellness" },
];

const MAX_INTERESTS = 20;
const MAX_LENGTH = 64;

/** Mirror the server's normalizer so the reader sees an inline error rather than a 422. */
function normalize(raw: string): string {
  return raw.split(/\s+/).filter(Boolean).join(" ").toLowerCase();
}

export function TastePanel() {
  const { me, config, tenantId, applyMe } = useSession();

  const baseline = useMemo(() => me?.interests ?? [], [me?.interests]);
  const [interests, setInterests] = useState<string[]>(baseline);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => setInterests(baseline), [baseline]);

  const dirty =
    interests.length !== baseline.length ||
    interests.some((value, index) => value !== baseline[index]);

  const toggle = (value: string) => {
    setInterests((current) =>
      current.includes(value)
        ? current.filter((entry) => entry !== value)
        : current.length >= MAX_INTERESTS
          ? current
          : [...current, value],
    );
  };

  const addDraft = () => {
    const value = normalize(draft);
    if (!value) return;
    if (value.length > MAX_LENGTH) {
      setError(`Keep each interest to ${MAX_LENGTH} characters or fewer.`);
      return;
    }
    if (interests.length >= MAX_INTERESTS) {
      setError(`You can keep up to ${MAX_INTERESTS} interests.`);
      return;
    }
    setError(null);
    setDraft("");
    setInterests((current) => (current.includes(value) ? current : [...current, value]));
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      // The revision is caller-minted and strictly increasing: echoing the current value back is
      // rejected, so always propose the next one and adopt whatever the server returns.
      const result = await updatePreferences(
        interests,
        (me?.preference_revision ?? 0) + 1,
        tenantId,
      );
      if (me) {
        applyMe({ ...me, interests: result.interests, preference_revision: result.revision });
      }
      setSaved(true);
      window.setTimeout(() => setSaved(false), 4000);
    } catch (saveError) {
      setError(readableError(saveError, "Your interests were not saved."));
    } finally {
      setSaving(false);
    }
  };

  const custom = interests.filter(
    (value) => !SUGGESTED.some((suggestion) => suggestion.value === value),
  );

  return (
    <div className="settings-panel">
      <header className="settings-panel__head">
        <h1>Interests</h1>
        <p>{releaseProfile(config) === "full"
          ? "A starting signal, not a filter. What you engage with keeps refining the order."
          : "Your saved interests. Use topic filters to choose which events you see."}</p>
      </header>

      <section className="settings-card">
        <h2 className="settings-card__title">Suggested</h2>
        <div className="chip-cloud">
          {SUGGESTED.map((suggestion) => {
            const active = interests.includes(suggestion.value);
            return (
              <button
                key={suggestion.value}
                type="button"
                className={`choice-chip${active ? " is-active" : ""}`}
                aria-pressed={active}
                onClick={() => toggle(suggestion.value)}
              >
                {suggestion.label}
              </button>
            );
          })}
        </div>
      </section>

      <section className="settings-card">
        <h2 className="settings-card__title">Anything else</h2>
        {custom.length > 0 ? (
          <div className="chip-cloud">
            {custom.map((value) => (
              <span key={value} className="choice-chip is-active">
                {value}
                <button
                  type="button"
                  className="choice-chip__remove"
                  aria-label={`Remove ${value}`}
                  onClick={() => toggle(value)}
                >
                  <X aria-hidden="true" />
                </button>
              </span>
            ))}
          </div>
        ) : null}
        <div className="settings-inline-add">
          <input
            type="text"
            value={draft}
            maxLength={MAX_LENGTH}
            placeholder="Board games, tango, urbanism…"
            aria-label="Add an interest"
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                addDraft();
              }
            }}
          />
          <button type="button" className="button button-quiet" onClick={addDraft}>
            Add
          </button>
        </div>
        <p className="settings-hint">
          {interests.length} of {MAX_INTERESTS} kept.
        </p>
      </section>

      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}

      <footer className="settings-actions">
        <span className="settings-actions__state" role="status">
          {saved ? (
            <>
              <Check aria-hidden="true" /> Interests saved.
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
            setInterests(baseline);
            setError(null);
          }}
        >
          Cancel
        </button>
        <button
          type="button"
          className="button button-primary"
          disabled={!dirty || saving}
          onClick={save}
        >
          {saving ? <LoaderCircle className="spin" aria-hidden="true" /> : null}
          Save interests
        </button>
      </footer>
    </div>
  );
}
