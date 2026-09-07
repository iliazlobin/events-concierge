import type { EventItem } from "./types.ts";

type PricedEvent = Pick<
  EventItem,
  "price_status" | "price_min_cents" | "price_max_cents" | "price_currency"
>;

const MAX_PUBLIC_PRICE_CENTS = 100_000_000;
const supportedCurrencies = new Set(Intl.supportedValuesOf("currency"));

function normalizedCents(value: number | null): number | null {
  return value !== null
    && Number.isSafeInteger(value)
    && value >= 1
    && value <= MAX_PUBLIC_PRICE_CENTS
    ? value
    : null;
}

function normalizedCurrency(value: string | null): string | null {
  const currency = value?.trim().toUpperCase() ?? "";
  return /^[A-Z]{3}$/.test(currency) && supportedCurrencies.has(currency)
    ? currency
    : null;
}

export function formatEventPrice(event: PricedEvent): string | null {
  if (event.price_status === "free") return "Free";
  if (event.price_status !== "paid") return null;

  const minimum = normalizedCents(event.price_min_cents);
  const maximum = normalizedCents(event.price_max_cents);
  const currency = normalizedCurrency(event.price_currency);
  if (
    minimum === null
    || maximum === null
    || minimum > maximum
    || !currency
  ) {
    return "Paid";
  }

  try {
    const formatter = new Intl.NumberFormat("en-US", {
      style: "currency",
      currency,
      currencyDisplay: "symbol",
    });
    const formattedMinimum = formatter.format(minimum / 100);
    return minimum === maximum
      ? formattedMinimum
      : `${formattedMinimum}–${formatter.format(maximum / 100)}`;
  } catch {
    return "Paid";
  }
}
