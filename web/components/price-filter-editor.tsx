"use client";

import { useRef, useState } from "react";

import { FilterChipPopover } from "@/components/filter-chip-popover";
import { PRICE_FILTER_OPTIONS } from "@/lib/filter-suggestions";
import type { PriceComparison, PriceFilter } from "@/lib/types";

export interface PriceSelection {
  price: PriceFilter;
  comparison: PriceComparison;
  minimum: string;
  maximum: string;
}

interface PriceFilterEditorProps {
  variant?: "chip" | "add";
  summary: string;
  title: string;
  hint: string;
  selection: PriceSelection;
  clearActionLabel?: string;
  removeLabel?: string;
  onApply: (selection: PriceSelection) => void;
  onRemove?: () => void;
}

const COMPARISONS: ReadonlyArray<{ value: PriceComparison; label: string }> = [
  { value: "any", label: "Any" },
  { value: "at-most", label: "Up to" },
  { value: "at-least", label: "At least" },
  { value: "exactly", label: "Exactly" },
  { value: "between", label: "Between" },
];

/** The same precision and maximum the catalog API accepts. */
const AMOUNT = /^\d{1,7}(?:\.\d{1,2})?$/;

function amountIsUsable(value: string): boolean {
  const trimmed = value.trim();
  return AMOUNT.test(trimmed) && Number(trimmed) > 0 && Number(trimmed) <= 1_000_000;
}

/** An amount is only meaningful for a category that can carry one. */
function categoryTakesAmount(price: PriceFilter): boolean {
  return price !== "free" && price !== "unknown";
}

function selectionIsComplete(selection: PriceSelection): boolean {
  const { comparison, minimum, maximum } = selection;
  if (!categoryTakesAmount(selection.price) || comparison === "any") return true;
  if (comparison === "at-most") return amountIsUsable(maximum);
  if (comparison === "between") {
    return amountIsUsable(minimum)
      && amountIsUsable(maximum)
      && Number(minimum) <= Number(maximum);
  }
  return amountIsUsable(minimum);
}

/**
 * The price filter, which is a category and an amount comparison rather than a
 * value picked from a list.
 *
 * Price is the one filter that cannot be several values at once: the API takes
 * one category and two bounds. Offering "at least", "exactly", and "between"
 * alongside the original ceiling covers what a multi-select would have been
 * used for, and does it in terms the server can actually answer.
 */
export function PriceFilterEditor({
  variant = "chip",
  summary,
  title,
  hint,
  selection,
  clearActionLabel,
  removeLabel,
  onApply,
  onRemove,
}: PriceFilterEditorProps) {
  const [draft, setDraft] = useState<PriceSelection>(selection);
  const firstAmountRef = useRef<HTMLInputElement>(null);
  const takesAmount = categoryTakesAmount(draft.price);
  const showsMinimum = draft.comparison !== "any" && draft.comparison !== "at-most";
  const showsMaximum = draft.comparison === "at-most" || draft.comparison === "between";

  const setAmount = (key: "minimum" | "maximum", value: string) => {
    setDraft((current) => ({ ...current, [key]: value.replace(/[^0-9.]/g, "") }));
  };

  const resolved: PriceSelection = {
    ...draft,
    // A category that cannot carry an amount drops one rather than keeping a
    // bound the reader can no longer see.
    comparison: takesAmount ? draft.comparison : "any",
    minimum: takesAmount && showsMinimum ? draft.minimum.trim() : "",
    maximum: takesAmount && showsMaximum ? draft.maximum.trim() : "",
  };

  /**
   * Price commits when the reader leaves, not as they type.
   *
   * Applying live would rewrite the chip's summary under an open popover, and a
   * wider chip shifts the strip that this popover is anchored to. An incomplete
   * comparison never applies at all -- which is what the old disabled Save was
   * expressing.
   */
  const commitDraft = () => {
    if (!selectionIsComplete(draft)) return;
    if (
      resolved.price === selection.price
      && resolved.comparison === selection.comparison
      && resolved.minimum === selection.minimum
      && resolved.maximum === selection.maximum
    ) return;
    onApply(resolved);
  };

  return (
    <FilterChipPopover
      variant={variant}
      chipLabel="Price"
      summary={summary}
      title={title}
      hint={hint}
      removeLabel={removeLabel}
      clearActionLabel={clearActionLabel}
      onOpen={() => setDraft(selection)}
      onDismiss={commitDraft}
      onRemove={onRemove}
    >
      <div className="price-filter-editor__segments" role="group" aria-label="Ticket type">
        {PRICE_FILTER_OPTIONS.map((option) => (
          <button
            key={option.value}
            className={draft.price === option.value ? "is-active" : ""}
            type="button"
            aria-pressed={draft.price === option.value}
            onClick={() => setDraft((current) => ({ ...current, price: option.value }))}
          >
            {option.label}
          </button>
        ))}
      </div>

      <div className="price-filter-editor__amount">
        <span id="price-filter-comparison">Amount</span>
        <div
          className="price-filter-editor__segments"
          role="group"
          aria-labelledby="price-filter-comparison"
        >
          {COMPARISONS.map((comparison) => (
            <button
              key={comparison.value}
              className={draft.comparison === comparison.value ? "is-active" : ""}
              type="button"
              disabled={!takesAmount}
              aria-pressed={draft.comparison === comparison.value}
              onClick={() => {
                setDraft((current) => ({ ...current, comparison: comparison.value }));
                window.requestAnimationFrame(() => firstAmountRef.current?.select());
              }}
            >
              {comparison.label}
            </button>
          ))}
        </div>

        {takesAmount && draft.comparison !== "any" ? (
          <div className="price-filter-editor__fields">
            {showsMinimum ? (
              <label>
                <span className="sr-only">
                  {draft.comparison === "between" ? "Lowest price" : "Price"}
                </span>
                <em aria-hidden="true">$</em>
                <input
                  ref={firstAmountRef}
                  value={draft.minimum}
                  inputMode="decimal"
                  placeholder="0"
                  maxLength={10}
                  onChange={(event) => setAmount("minimum", event.target.value)}
                />
              </label>
            ) : null}
            {showsMinimum && showsMaximum ? <b aria-hidden="true">to</b> : null}
            {showsMaximum ? (
              <label>
                <span className="sr-only">
                  {draft.comparison === "between" ? "Highest price" : "Price"}
                </span>
                <em aria-hidden="true">$</em>
                <input
                  ref={showsMinimum ? undefined : firstAmountRef}
                  value={draft.maximum}
                  inputMode="decimal"
                  placeholder="0"
                  maxLength={10}
                  onChange={(event) => setAmount("maximum", event.target.value)}
                />
              </label>
            ) : null}
          </div>
        ) : null}

        {takesAmount ? null : (
          <p className="filter-chip-editor__empty">
            {draft.price === "free" ? "Free events carry no amount" : "An unlisted price has no amount"}
          </p>
        )}
      </div>
    </FilterChipPopover>
  );
}
