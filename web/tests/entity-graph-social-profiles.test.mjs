import test from "node:test";
import assert from "node:assert/strict";
import { socialProfileCards } from "../lib/entity-social-profiles.ts";

const source = (key, status = "fresh") => ({provider_key: key, source_url: "https://x.com/builder", display_name: "X API", status, fetched_at: "2026-10-04T00:00:00Z"});
const fact = (provider_key, fact_key, value, value_url = null) => ({provider_key, fact_key, value, value_url});

test("platform facts stay separate and failed refresh retains dated snapshot", () => {
  const detail = {external_sources:[source("x_public_api", "failed"), source("instagram_public_api"), {...source("x_profile"),fetched_at:null}], external_facts:[
    fact("x_public_api","description","AI events"), fact("x_public_api","followers","6412"),
    fact("instagram_public_api","followers","4300"), fact("x_public_api","avatar","Image","https://pbs.twimg.com/image.png")
  ]};
  const cards=socialProfileCards(detail);
  assert.equal(cards.length,2);
  assert.equal(cards[0].followers,"6,412");
  assert.equal(cards[1].followers,"4,300");
  assert.equal(cards[0].source.status,"failed");
  assert.equal(cards[0].avatar,"https://pbs.twimg.com/image.png");
});

test("untrusted images and malformed metrics cannot render", () => {
  const card=socialProfileCards({external_sources:[source("x_public_api")],external_facts:[
    fact("x_public_api","avatar","Image","https://pbs.twimg.com.evil.test/image.png"),
    fact("x_public_api","followers","<script>")
  ]})[0];
  assert.equal(card.avatar,null);
  assert.equal(card.followers,null);
});

test("link-only and unfetched API rows do not claim fetched data", () => {
  assert.deepEqual(socialProfileCards({external_sources:[{...source("x_public_api"),fetched_at:null}],external_facts:[]}),[]);
});
