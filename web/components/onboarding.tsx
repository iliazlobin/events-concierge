"use client";

import { ArrowRight, LoaderCircle } from "lucide-react";
import { FormEvent, useState } from "react";

interface OnboardingProps {
  busy: boolean;
  error: string | null;
  onSubmit: (email: string) => Promise<void>;
}

export function Onboarding({ busy, error, onSubmit }: OnboardingProps) {
  const [email, setEmail] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (email.trim()) void onSubmit(email.trim());
  };

  return (
    <main className="onboarding">
      <div className="onboarding__mark" aria-hidden="true">
        <span />
        <span />
      </div>
      <section>
        <p className="onboarding__kicker">Events Concierge</p>
        <h1>Find events</h1>
        <form onSubmit={submit}>
          <label>
            <span className="sr-only">Email address</span>
            <input
              type="email"
              required
              autoFocus
              autoComplete="email"
              placeholder="you@example.com"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </label>
          <button type="submit" disabled={busy}>
            {busy ? <LoaderCircle className="spin" aria-hidden="true" /> : null}
            Continue
            {!busy ? <ArrowRight aria-hidden="true" /> : null}
          </button>
        </form>
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <small>Local preview. No real registrations.</small>
      </section>
    </main>
  );
}
