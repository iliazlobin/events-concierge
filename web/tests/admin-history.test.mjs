import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  adminHistoryLocationFromUrl,
  adminHistoryUrl,
} from "../lib/admin-history.ts";

const adminConsole = readFileSync(
  new URL("../components/admin/admin-console.tsx", import.meta.url),
  "utf8",
);

test("admin history URLs round-trip every workspace and source investigation", () => {
  for (const tab of ["overview", "pipeline", "sources", "runs", "commands"]) {
    const url = adminHistoryUrl(
      { tab, sourceKey: null },
      "https://events.test/admin?campaign=ops#workspace",
    );
    const parsed = new URL(url, "https://events.test");
    assert.equal(parsed.searchParams.get("campaign"), "ops");
    assert.equal(adminHistoryLocationFromUrl(`https://events.test${url}`).tab, tab);
  }

  const sourceUrl = adminHistoryUrl(
    { tab: "sources", sourceKey: "luma-sf" },
    "https://events.test/admin?tab=pipeline",
  );
  assert.deepEqual(
    adminHistoryLocationFromUrl(`https://events.test${sourceUrl}`),
    { tab: "sources", sourceKey: "luma-sf" },
  );
});

test("admin workspace navigation pushes entries and popstate restores the URL state", () => {
  assert.match(adminConsole, /window\.history\.pushState\(/);
  assert.match(adminConsole, /adminHistoryUrl\(nextLocation, window\.location\.href\)/);
  assert.match(adminConsole, /window\.addEventListener\("popstate", handlePopState\)/);
  assert.match(
    adminConsole,
    /const handlePopState = \(\) => \{[\s\S]*?adminHistoryLocationFromUrl\(window\.location\.href\)[\s\S]*?setTab\(location\.tab\)[\s\S]*?setSelectedSourceKey\(location\.sourceKey\)/,
  );
  assert.match(adminConsole, /navigateAdmin\("sources"\)/);
});
