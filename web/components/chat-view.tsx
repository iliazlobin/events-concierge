"use client";

import { ArrowUp, LoaderCircle, Sparkles } from "lucide-react";
import { FormEvent, useRef, useState } from "react";

import { ChatBlocks } from "@/components/chat-blocks";
import { EventList } from "@/components/event-list";
import type { SelectionEntry } from "@/lib/agent-stream";
import type { ChatTurn, EventEntityReference } from "@/lib/types";

interface ChatViewProps {
  turns: ChatTurn[];
  selected: ReadonlySet<string>;
  selectionEntries: SelectionEntry[];
  onSelectionChange: (entries: SelectionEntry[], mode: "toggle" | "range" | "replace") => void;
  busy: boolean;
  error: string | null;
  onSubmit: (text: string) => Promise<void>;
  onSourceSelect: (sourceKey: string) => void;
  onFacetSelect: (value: string) => void;
  onEntitySelect: (reference: EventEntityReference) => void;
  onTopicSelect: (topic: string) => void;
}

const PROMPTS = [
  "An AI meetup this week",
  "Free live music Friday night",
  "Something creative this weekend",
];

export function ChatView({
  turns,
  selected,
  selectionEntries,
  onSelectionChange,
  busy,
  error,
  onSubmit,
  onSourceSelect,
  onFacetSelect,
  onEntitySelect,
  onTopicSelect,
}: ChatViewProps) {
  const [text, setText] = useState("");
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);

  /**
   * Adopt a suggested prompt.
   *
   * Filling the box while focus stays on the chip leaves the suggestion looking committed when
   * it is really just a draft: to change a word you have to click into the field first, and the
   * focus ring is still on a button that does nothing more. Move focus to the composer and
   * select what was inserted, so the next keystroke replaces it and Enter sends it.
   */
  const applySuggestion = (prompt: string) => {
    setText(prompt);
    const composer = composerRef.current;
    if (!composer) return;
    composer.focus();
    // The value lands on the next render, so selecting has to wait for it.
    requestAnimationFrame(() => composer.select());
  };

  /**
   * Scroll to whatever a context chip refers to, and flash it.
   *
   * Without this the bar is a graveyard: you can see what is in context but have no way back to
   * it, and with several selections you lose track of which card is which.
   */
  const locate = (selectionId: string) => {
    const target = document.querySelector(`[data-selection-anchor="${CSS.escape(selectionId)}"]`);
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.classList.remove("ec-locating");
    // Force a reflow so the animation restarts when the same chip is clicked twice.
    void (target as HTMLElement).offsetWidth;
    target.classList.add("ec-locating");
  };

  /**
   * Re-open a previous prompt for editing, with the context it was asked with.
   *
   * Rerunning an earlier question is the commonest reason to reselect the same cards, and
   * rebuilding a selection by hand is the tedious part. Restoring both makes "same question, one
   * word different" a two-second edit instead of a re-pick.
   */
  const recall = (turn: ChatTurn) => {
    setText(turn.text);
    onSelectionChange(turn.selection ?? [], "replace");
    const composer = composerRef.current;
    if (!composer) return;
    composer.focus();
    requestAnimationFrame(() => composer.select());
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const prompt = text.trim();
    if (!prompt || busy) return;
    setText("");
    void onSubmit(prompt);
  };

  const empty = turns.length === 0;

  return (
    <section className={`chat-view${empty ? " is-empty" : ""}`}>
      {empty ? (
        <div className="chat-hero">
          <span className="chat-hero__orb" aria-hidden="true">
            <Sparkles />
          </span>
          <p>YOUR WEEK, CURATED</p>
          <h1>What sounds good?</h1>
          <span>Ask naturally. We will keep the answer grounded in current events.</span>
        </div>
      ) : (
        <div className="chat-thread" role="log" aria-live="off">
          {turns.map((turn) => (
            <article key={turn.id} className={`chat-turn chat-turn--${turn.role}`}>
              <p className="chat-turn__role">
                {turn.role === "user" ? "You" : "Concierge"}
              </p>
              {turn.role === "user" ? (
                <button
                  type="button"
                  className="chat-turn__recall"
                  title={
                    turn.selection?.length
                      ? `Ask again — restores ${turn.selection.length} in context`
                      : "Ask again"
                  }
                  onClick={() => recall(turn)}
                >
                  <span className="chat-turn__copy">{turn.text}</span>
                </button>
              ) : null}
              {/* The context a question carried belongs with the question, not summarised as a
                  number: three turns later "4 in context" tells you nothing, while the chips tell
                  you exactly which cards the answer was about. Kept outside the recall button --
                  a button cannot nest buttons, and each chip navigates on its own. */}
              {turn.role === "user" && turn.selection?.length ? (
                <div className="chat-turn__context">
                  {turn.selection.map((entry) => (
                    <button
                      key={entry.id}
                      type="button"
                      className="chat-turn__chip"
                      title={`Go to "${entry.label}"`}
                      onClick={() => locate(entry.id)}
                    >
                      <span className="chat-turn__chip-kind" aria-hidden="true">
                        {entry.kind === "event" ? "◆" : entry.kind === "entity" ? "◇" : "❝"}
                      </span>
                      <span className="chat-turn__chip-label">{entry.label}</span>
                    </button>
                  ))}
                </div>
              ) : null}
              {turn.blocks?.length ? (
                <ChatBlocks
                  blocks={turn.blocks}
                  onAction={(action) => {
                    if (action.type !== "ask" || busy) return;
                    void onSubmit(action.text);
                  }}
                  onSourceSelect={onSourceSelect}
                  onFacetSelect={onFacetSelect}
                  onEntitySelect={onEntitySelect}
                  onTopicSelect={onTopicSelect}
                  selected={selected}
                  onSelectionChange={onSelectionChange}
                />
              ) : turn.text && turn.role !== "user" ? (
                <p className="chat-turn__copy">{turn.text}</p>
              ) : null}
              {turn.trace?.length ? (
                <details className="chat-trace">
                  <summary>
                    {turn.trace.filter((step) => step.status).length || turn.trace.length} step
                    {turn.trace.length === 1 ? "" : "s"}
                  </summary>
                  <ul>
                    {turn.trace.map((step, index) => (
                      <li key={index}>
                        <code>{step.tool}</code>
                        {step.summary ? ` · ${step.summary}` : null}
                        {step.status && step.status !== "ok" ? ` · ${step.status}` : null}
                      </li>
                    ))}
                  </ul>
                </details>
              ) : null}
              {turn.pending && !turn.blocks?.length ? (
                <p className="chat-turn__copy chat-turn__copy--pending">Working on it</p>
              ) : null}
              {turn.role === "assistant" && turn.error ? (
                <button
                  type="button"
                  className="chat-turn__retry"
                  disabled={busy}
                  onClick={() => {
                    const asked = [...turns].reverse().find((candidate) => candidate.role === "user");
                    if (!asked) return;
                    onSelectionChange(asked.selection ?? [], "replace");
                    void onSubmit(asked.text);
                  }}
                >
                  Try again
                </button>
              ) : null}
              {turn.items ? (
                <EventList
                  events={turn.items}
                  compact
                  expandedId={expandedId}
                  onExpandedChange={setExpandedId}
                  onSourceSelect={onSourceSelect}
                  onFacetSelect={onFacetSelect}
                  onEntitySelect={onEntitySelect}
                  onTopicSelect={onTopicSelect}
                  emptyTitle="No strong matches"
                  emptyCopy="Try loosening the date, place, or price in your request."
                />
              ) : null}
            </article>
          ))}
          {busy ? (
            <div className="chat-thinking">
              <span />
              <span />
              <span />
              Searching the catalog
            </div>
          ) : null}
        </div>
      )}

      <div className="chat-composer-wrap">
        {/* What is in context has to be visible at the point of asking. A selection the user
            cannot see is a selection they will forget they made, and every answer after that
            looks inexplicably narrow. */}
        {selected.size ? (
          <div className="chat-context" role="status" aria-live="polite">
            <span className="chat-context__count">{selected.size} in context</span>
            <span className="chat-context__items">
              {selectionEntries.map((entry) => (
                <span key={entry.id} className="chat-context__chip">
                  <button
                    type="button"
                    className="chat-context__go"
                    title={`Go to "${entry.label}"`}
                    onClick={() => locate(entry.id)}
                  >
                    <span className="chat-context__kind" aria-hidden="true">
                      {entry.kind === "event" ? "◆" : entry.kind === "entity" ? "◇" : "❝"}
                    </span>
                    <span className="chat-context__label">{entry.label}</span>
                  </button>
                  <button
                    type="button"
                    className="chat-context__drop"
                    aria-label={`Remove ${entry.label} from the conversation`}
                    title="Remove"
                    onClick={() => onSelectionChange([entry], "toggle")}
                  >
                    ×
                  </button>
                </span>
              ))}
            </span>
            <button
              type="button"
              className="chat-context__clear"
              onClick={() => onSelectionChange([], "replace")}
            >
              Clear
            </button>
          </div>
        ) : null}
        {empty ? (
          <div className="prompt-row">
            {PROMPTS.map((prompt) => (
              <button key={prompt} type="button" onClick={() => applySuggestion(prompt)}>
                {prompt}
              </button>
            ))}
          </div>
        ) : null}
        <form className="chat-composer" onSubmit={submit}>
          <label>
            <span className="sr-only">Ask your concierge</span>
            <textarea
              ref={composerRef}
              rows={1}
              maxLength={2000}
              value={text}
              onChange={(event) => setText(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
              placeholder="Ask for an event…"
            />
          </label>
          <button type="submit" disabled={!text.trim() || busy} aria-label="Send">
            {busy
              ? <LoaderCircle className="spin" aria-hidden="true" />
              : <ArrowUp aria-hidden="true" />}
          </button>
        </form>
        {error ? <p className="composer-error" role="alert">{error}</p> : null}
      </div>
    </section>
  );
}
