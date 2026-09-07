"use client";

import { Check, ChevronDown, X } from "lucide-react";
import { type FocusEvent, useRef, useState } from "react";

import type { PriceFilter } from "@/lib/types";

interface PriceFilterControlProps {
  controlId?: string;
  price: PriceFilter;
  maximumDollars: string;
  onChange: (price: PriceFilter, maximumDollars: string) => void;
}

const OPTIONS: Array<{ value: PriceFilter; label: string; copy: string }> = [
  { value: "any", label: "Any price", copy: "Free, paid, and unlisted" },
  { value: "free", label: "Free", copy: "No ticket price" },
  { value: "paid", label: "Paid", copy: "Known paid admission" },
  { value: "unknown", label: "Unlisted", copy: "The source did not publish a price" },
];

function normalizedMaximum(value: string): string {
  const amount = Number(value);
  if (!Number.isFinite(amount) || amount <= 0) return value;
  return amount.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function summary(price: PriceFilter, maximumDollars: string): string {
  const maximum = maximumDollars.trim();
  if (price === "free") return "Free";
  if (price === "unknown") return "Unlisted";
  if (maximum) {
    const ceiling = `≤ $${normalizedMaximum(maximum)}`;
    return price === "paid" ? `Paid ${ceiling}` : ceiling;
  }
  return price === "paid" ? "Paid" : "Any price";
}

export function PriceFilterControl({
  controlId,
  price,
  maximumDollars,
  onChange,
}: PriceFilterControlProps) {
  const controlRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);

  const handleBlur = (event: FocusEvent<HTMLDivElement>) => {
    const control = event.currentTarget;
    window.setTimeout(() => {
      if (!control.contains(document.activeElement)) setOpen(false);
    }, 0);
  };

  const choosePrice = (next: PriceFilter) => {
    const nextMaximum = next === "free" || next === "unknown" ? "" : maximumDollars;
    onChange(next, nextMaximum);
  };

  return (
    <div
      className={`price-filter${open ? " is-open" : ""}`}
      ref={controlRef}
      onBlur={handleBlur}
    >
      <button
        id={controlId}
        className="price-filter__trigger"
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span>Price</span>
        <strong>{summary(price, maximumDollars)}</strong>
        <ChevronDown aria-hidden="true" />
      </button>

      {open ? (
        <div className="price-filter__popover" role="dialog" aria-label="Filter by price">
          <div className="price-filter__options" role="radiogroup" aria-label="Price type">
            {OPTIONS.map((option) => (
              <button
                key={option.value}
                type="button"
                role="radio"
                aria-checked={price === option.value}
                className={price === option.value ? "is-selected" : ""}
                onClick={() => choosePrice(option.value)}
              >
                <Check aria-hidden="true" />
                <span>
                  <strong>{option.label}</strong>
                  <small>{option.copy}</small>
                </span>
              </button>
            ))}
          </div>

          <label className="price-filter__maximum">
            <span>
              <strong>Maximum ticket price</strong>
              <small>Includes free events when “Any price” is selected.</small>
            </span>
            <span className="price-filter__input">
              <i aria-hidden="true">$</i>
              <input
                type="number"
                inputMode="decimal"
                min="0.01"
                max="1000000"
                step="0.01"
                placeholder="No maximum"
                aria-label="Maximum ticket price in dollars"
                value={maximumDollars}
                disabled={price === "free" || price === "unknown"}
                onChange={(event) => onChange(price, event.target.value)}
              />
              {maximumDollars ? (
                <button
                  type="button"
                  aria-label="Clear maximum ticket price"
                  onClick={() => onChange(price, "")}
                >
                  <X aria-hidden="true" />
                </button>
              ) : null}
            </span>
          </label>
        </div>
      ) : null}
    </div>
  );
}
