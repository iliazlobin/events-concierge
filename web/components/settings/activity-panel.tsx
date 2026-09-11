"use client";

import { CalendarClock, Inbox, LoaderCircle } from "lucide-react";
import { useEffect, useState } from "react";

import { useSession } from "@/components/session-provider";
import {
  ApiError,
  getRegistrations,
  getRequestHistory,
  getTasks,
  readableError,
  withdrawRegistration,
} from "@/lib/api";
import { releaseProfile } from "@/lib/release-profile";
import type { RegistrationSummary, RequestSummary, TaskSummary } from "@/lib/types";

function when(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/**
 * What the concierge has been asked for, what it registered, and what still needs a person.
 *
 * All three reads already existed on the API and had no caller in this client, which meant a product
 * that registers on your behalf could not show you what it had done.
 */
export function ActivityPanel() {
  const { tenantId, config } = useSession();
  const fullRelease = releaseProfile(config) === "full";
  const [requests, setRequests] = useState<RequestSummary[]>([]);
  const [registrations, setRegistrations] = useState<RegistrationSummary[]>([]);
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [withdrawing, setWithdrawing] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    if (!fullRelease) return;
    let cancelled = false;
    setLoading(true);
    void Promise.all([
      getRequestHistory(tenantId),
      getRegistrations(tenantId),
      getTasks(tenantId),
    ])
      .then(([askPage, registrationPage, taskPage]) => {
        if (cancelled) return;
        setRequests(askPage.items);
        setRegistrations(registrationPage.items);
        setTasks(taskPage.items);
        setError(null);
      })
      .catch((loadError: unknown) => {
        if (!cancelled) setError(readableError(loadError, "Your activity could not be loaded."));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [fullRelease, tenantId]);

  const withdraw = async (registration: RegistrationSummary) => {
    if (!fullRelease) return;
    setWithdrawing(registration.canonical_event_id);
    setNotice(null);
    try {
      await withdrawRegistration(
        registration.canonical_event_id,
        crypto.randomUUID(),
        tenantId,
      );
      setNotice("Withdrawal accepted. Your calendar reconciles shortly.");
    } catch (withdrawError) {
      // A conflict means the registration saga has not reached a withdrawable stage yet.
      setNotice(
        withdrawError instanceof ApiError && withdrawError.status === 409
          ? "That registration is still completing. Try again shortly."
          : readableError(withdrawError, "The withdrawal was not accepted."),
      );
    } finally {
      setWithdrawing(null);
    }
  };

  if (!fullRelease) {
    return <div className="settings-panel">
      <h1>Event discovery</h1>
      <p>Browse events and visit their provider pages for registration.</p>
      <a href="/?view=events">Browse events</a>
    </div>;
  }

  if (loading) {
    return (
      <div className="settings-state" role="status">
        <LoaderCircle className="spin" aria-hidden="true" />
        <p>Loading your activity…</p>
      </div>
    );
  }

  return (
    <div className="settings-panel">
      <header className="settings-panel__head">
        <h1>Activity</h1>
        <p>What you asked for, what the concierge did, and anything still waiting on you.</p>
      </header>

      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}
      {notice ? (
        <p className="settings-notice" role="status">
          {notice}
        </p>
      ) : null}

      <section className="settings-card">
        <h2 className="settings-card__title">A step from you</h2>
        {tasks.length === 0 ? (
          <p className="settings-empty">
            <Inbox aria-hidden="true" />
            Nothing needs you right now.
          </p>
        ) : (
          <ul className="activity-list">
            {tasks.map((task) => (
              <li key={task.task_id}>
                <p className="activity-list__title">{task.title}</p>
                <p className="activity-list__meta">
                  {when(task.start_at)}
                  {task.venue_name ? ` · ${task.venue_name}` : ""}
                </p>
                <p className="activity-list__meta">{task.reason.replaceAll("_", " ")}</p>
                <a
                  className="button button-quiet"
                  href={task.deep_link}
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  Finish on the event site
                </a>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="settings-card">
        <h2 className="settings-card__title">Registered for you</h2>
        {registrations.length === 0 ? (
          <p className="settings-empty">
            <CalendarClock aria-hidden="true" />
            Nothing registered yet.
          </p>
        ) : (
          <ul className="activity-list">
            {registrations.map((registration) => (
              <li key={registration.canonical_event_id}>
                <p className="activity-list__title">{registration.title}</p>
                <p className="activity-list__meta">
                  {when(registration.start_at)}
                  {registration.venue_name ? ` · ${registration.venue_name}` : ""}
                  {registration.city ? ` · ${registration.city}` : ""}
                </p>
                <p className="activity-list__meta">
                  <span className="activity-state">{registration.state.replaceAll("_", " ")}</span>
                  {registration.conflict_warning ? " · overlaps something else" : ""}
                </p>
                {registration.can_withdraw ? (
                  <button
                    type="button"
                    className="button button-quiet-danger"
                    disabled={withdrawing === registration.canonical_event_id}
                    onClick={() => withdraw(registration)}
                  >
                    {withdrawing === registration.canonical_event_id ? "Withdrawing…" : "Withdraw"}
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="settings-card">
        <h2 className="settings-card__title">What you asked for</h2>
        {requests.length === 0 ? (
          <p className="settings-empty">
            <Inbox aria-hidden="true" />
            No saved briefs yet.
          </p>
        ) : (
          <ul className="activity-list">
            {requests.map((request) => (
              <li key={request.request_id}>
                <p className="activity-list__title">{request.text}</p>
                <p className="activity-list__meta">
                  {when(request.created_at)} ·{" "}
                  <span className="activity-state">{request.state.replaceAll("_", " ")}</span>
                </p>
                {request.outcome ? (
                  <p className="activity-list__meta">Chose: {request.outcome.title}</p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
