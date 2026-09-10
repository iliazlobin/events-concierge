import assert from "node:assert/strict";
import { test } from "node:test";
import { inspectorLocation, inspectorLocationUrl } from "../lib/admin-inspector-navigation.ts";

const options = { selectionParam: "source_selection", panelParam: "source_inspector", panels: ["overview", "evidence", "operations"], defaultPanel: "overview" };
test("inspector links preserve workspace filters and restore selection and panel", () => {
  const url = inspectorLocationUrl("http://localhost/admin?tab=sources&region=bay#registry", options, "library-events", "evidence");
  assert.deepEqual(inspectorLocation(new URL(url, "http://localhost").href, options), { selection: "library-events", panel: "evidence" });
  assert.match(url, /region=bay/);
  assert.match(url, /#registry$/);
});
test("unknown panels and oversized or control-character selections fall back safely", () => {
  for (const selection of ["x".repeat(513), "bad\nvalue"]) {
    const url = new URL("http://localhost/admin");
    url.searchParams.set("source_selection", selection);
    url.searchParams.set("source_inspector", "execute");
    assert.deepEqual(inspectorLocation(url.href, options), { selection: null, panel: "overview" });
  }
});
test("closing one inspector keeps unrelated diagram and command identities", () => {
  const url = inspectorLocationUrl("http://localhost/admin?source_selection=library-events&source_inspector=evidence&component=catalog&command=retained", options, null, "overview");
  assert.equal(url, "/admin?component=catalog&command=retained");
});
