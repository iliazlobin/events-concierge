import type { CatalogPage } from "./types.ts";

/** Read bounded batches; every successful page remains usable after a later failure. */
export async function loadCatalogRemainder(
  initialCursor: string,
  readPage: (cursor: string) => Promise<CatalogPage>,
  onPage: (page: CatalogPage) => void,
  isCurrent: () => boolean,
  maxPages = 40,
): Promise<void> {
  const seen = new Set<string>();
  let cursor: string | null = initialCursor;
  for (let page = 0; cursor !== null && page < maxPages; page += 1) {
    if (!isCurrent()) return;
    seen.add(cursor);
    const result = await readPage(cursor);
    if (!isCurrent()) return;
    if (result.next_cursor !== null && seen.has(result.next_cursor)) {
      throw new Error("Catalog pagination did not advance. Refresh the map to try again.");
    }
    onPage(result);
    cursor = result.next_cursor;
  }
}
