import assert from "node:assert/strict";
import test from "node:test";
import { loadCatalogRemainder } from "../lib/catalog-pagination.ts";

const page = (next_cursor) => ({ items: [], next_cursor, providers: [], topic_facets: [] });

test("one request follows all remaining cursors, including empty pages", async () => {
  const reads = [];
  const appended = [];
  await loadCatalogRemainder("first", async (cursor) => {
    reads.push(cursor);
    return page(cursor === "first" ? "second" : null);
  }, (result) => appended.push(result.next_cursor), () => true);
  assert.deepEqual(reads, ["first", "second"]);
  assert.deepEqual(appended, ["second", null]);
});

test("failure preserves successful pages and their continuation for retry", async () => {
  const appended = [];
  await assert.rejects(loadCatalogRemainder("first", async (cursor) => {
    if (cursor === "second") throw new Error("unavailable");
    return page("second");
  }, (result) => appended.push(result.next_cursor), () => true), /unavailable/);
  assert.deepEqual(appended, ["second"]);
});

test("filter or view changes discard an in-flight page and stop further reads", async () => {
  let current = true;
  const appended = [];
  await loadCatalogRemainder("first", async () => {
    current = false;
    return page("second");
  }, (result) => appended.push(result), () => current);
  assert.deepEqual(appended, []);
});

test("cursor cycles fail instead of claiming complete coverage", async () => {
  const appended = [];
  await assert.rejects(loadCatalogRemainder("first", async (cursor) => page(
    cursor === "first" ? "second" : "first",
  ), (result) => appended.push(result.next_cursor), () => true), /did not advance/);
  assert.deepEqual(appended, ["second"]);
});

test("large catalogs stop at the request bound and preserve a continuation", async () => {
  const appended = [];
  await loadCatalogRemainder("0", async (cursor) => page(String(Number(cursor) + 1)),
    (result) => appended.push(result.next_cursor), () => true, 2);
  assert.deepEqual(appended, ["1", "2"]);
});
