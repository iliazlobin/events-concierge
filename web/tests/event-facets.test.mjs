import assert from "node:assert/strict";
import test from "node:test";

import {
  eventFacetSignals,
  eventFormatLabel,
  eventRegistrationCtaLabel,
  eventRegistrationLabel,
  safeEntityProfile,
} from "../lib/event-facets.ts";

function location(overrides = {}) {
  return {
    venue_name: null,
    city: null,
    latitude: null,
    longitude: null,
    ...overrides,
  };
}

test("event format requires explicit online venue text and otherwise uses real location evidence", () => {
  assert.equal(
    eventFormatLabel(location({
      venue_name: "Online via Zoom",
      city: "San Francisco",
      latitude: 37.7749,
      longitude: -122.4194,
    })),
    "Online",
  );
  assert.equal(
    eventFormatLabel(location({ venue_name: "Virtual event" })),
    "Online",
  );
  assert.equal(
    eventFormatLabel(location({ venue_name: "Virtual Reality Lab" })),
    "In person",
  );
  assert.equal(
    eventFormatLabel(location({ venue_name: "Online Trading Academy" })),
    "In person",
  );
  assert.equal(
    eventFormatLabel(location({ city: "San Francisco" })),
    "In person",
  );
  assert.equal(
    eventFormatLabel(location({ latitude: 37.7749, longitude: -122.4194 })),
    "In person",
  );
});

test("event format rejects placeholder text, partial coordinates, and out-of-range coordinates", () => {
  assert.equal(eventFormatLabel(location({ venue_name: "Location TBA" })), null);
  assert.equal(eventFormatLabel(location({ city: "Unknown" })), null);
  assert.equal(
    eventFormatLabel(location({ latitude: 37.7749, longitude: null })),
    null,
  );
  assert.equal(
    eventFormatLabel(location({ latitude: 91, longitude: -122.4194 })),
    null,
  );
});

test("registration status produces truthful status and action labels", () => {
  const cases = [
    ["open", "Registration open", "Sign up"],
    ["waitlist", "Waitlist", "Join waitlist"],
    ["sold_out", "Sold out", "View details"],
    ["unknown", null, "View event"],
  ];
  for (const [registration_status, status, action] of cases) {
    const event = { registration_status };
    assert.equal(eventRegistrationLabel(event), status);
    assert.equal(eventRegistrationCtaLabel(event), action);
  }
});

test("facet signals favor title evidence, use a fixed vocabulary, and stop at four", () => {
  assert.deepEqual(
    eventFacetSignals({
      title: "AI Founders Meetup",
      description: "A hands-on family workshop with live music and local art.",
    }),
    ["Meetup", "Founders", "AI", "Music"],
  );
  assert.deepEqual(
    eventFacetSignals({
      title: "Saturday gathering",
      description: "Live jazz, gallery art, and activities for families.",
    }),
    ["Music", "Arts", "Family"],
  );
});

test("facet signals use word boundaries, deduplicate repeated mentions, and do not guess", () => {
  assert.deepEqual(
    eventFacetSignals({
      title: "Developer Workshop Workshop",
      description: "Hands-on software training for developers.",
    }),
    ["Tech", "Workshop"],
  );
  assert.deepEqual(
    eventFacetSignals({
      title: "Retail strategy evening",
      description: "A conversation at the Virtual Reality Lab.",
    }),
    [],
  );
  assert.deepEqual(
    eventFacetSignals({
      title: "Neighborhood gathering",
      description: "Coffee and conversation.",
    }),
    [],
  );
});

test("entity profiles accept direct LinkedIn person and company URLs only", () => {
  assert.deepEqual(
    safeEntityProfile(
      "https://www.linkedin.com/in/john-maeda?trk=event#profile",
      "person",
    ),
    {
      url: "https://www.linkedin.com/in/john-maeda",
      network: "linkedin",
    },
  );
  assert.deepEqual(
    safeEntityProfile("https://linkedin.com/company/github/", "organization"),
    {
      url: "https://linkedin.com/company/github/",
      network: "linkedin",
    },
  );

  assert.equal(
    safeEntityProfile(
      "https://www.linkedin.com/search/results/people/?keywords=John%20Maeda",
      "person",
    ),
    null,
  );
  assert.equal(
    safeEntityProfile("https://www.linkedin.com/company/github", "person"),
    null,
  );
  assert.equal(
    safeEntityProfile("https://www.linkedin.com/in/john-maeda", "organization"),
    null,
  );
});

test("organization profiles allow canonical HTTPS sites while people require LinkedIn", () => {
  assert.deepEqual(
    safeEntityProfile("https://github.com/?ref=event#speakers", "organization"),
    {
      url: "https://github.com/?ref=event",
      network: "website",
    },
  );
  assert.equal(
    safeEntityProfile("https://johnmaeda.com/", "person"),
    null,
  );
  assert.equal(
    safeEntityProfile("http://github.com/", "organization"),
    null,
  );
  assert.equal(
    safeEntityProfile("javascript:alert(1)", "organization"),
    null,
  );
  assert.equal(
    safeEntityProfile("https://user:secret@example.com/", "organization"),
    null,
  );
  assert.equal(
    safeEntityProfile("https://example.com/\u0000bad", "organization"),
    null,
  );
});
