# Social profile enrichment

The entity-intelligence worker can read official APIs for exact social links already published
by event sources. Both adapters are disabled by default; live provider access is not yet configured.

| Provider | Saved fields | Access |
| --- | --- | --- |
| [X](https://docs.x.com/x-api/users/get-user-by-username) | Public bio, avatar URL, follower count | Developer bearer token; protected profiles excluded |
| [Instagram Business Discovery](https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/business_discovery) | Public bio, follower count | Facebook User token and professional Instagram account ID; Business/Creator targets only |
| LinkedIn, TikTok, YouTube | Profile link | No API fetch |

## Run

Apply the reviewed migration `0200` through the normal [release procedure](operations/README.md).
Mount credentials only into the entity-intelligence worker. Configure its environment:

| Setting | Value |
| --- | --- |
| `EC_ENTITY_INTELLIGENCE_ENABLED` | `true` |
| `EC_X_PROFILE_API_ENABLED` | `true` when X access is approved |
| `EC_X_PROFILE_BEARER_TOKEN_FILE` | Absolute mounted secret path |
| `EC_INSTAGRAM_PROFILE_API_ENABLED` | `true` when Meta access is approved |
| `EC_INSTAGRAM_PROFILE_ACCESS_TOKEN_FILE` | Absolute mounted secret path |
| `EC_INSTAGRAM_PROFILE_ACCOUNT_ID` | Your app user's professional Instagram account ID |
| `EC_INSTAGRAM_PROFILE_API_VERSION` | `v26.0` by default |
| `EC_SOCIAL_PROFILE_DAILY_LIMIT` | `100` reservations per provider per UTC day by default |
| `EC_SOCIAL_PROFILE_REFRESH_SECONDS` | `86400` by default; minimum one day |

```sh
python -m events_concierge.workers.entity_intelligence
```

Meta requires `instagram_basic`, `instagram_manage_insights` and `pages_read_engagement`;
additional permissions may apply when Page roles come through Business Manager. Validate access
with one known professional profile before enabling ordinary batches. No tokens in Git, URLs or logs.

## Contract

- Only catalog identities with an exact canonical profile and an imported social link are eligible.
  Name-only entities stay source scoped. Links remain assertions from an event source.
- Imported `x_profile` / `instagram_profile` rows and fetched `*_public_api` snapshots are separate.
  One snapshot per entity and platform; no entity merging or social-account graph nodes.
- Imports queue refresh rows without changing active leases; migration backfills existing links once.
- Each request reserves the shared daily allowance before egress, including failed/crashed attempts.
  Claims expire after 60 seconds; API work times out after 25 seconds. Stale or changed-link results
  cannot publish. Provider account IDs must remain stable across successful refreshes.
- Daily refreshes and delayed retries preserve the last successful facts and timestamp.
  Profile & sources shows each destination once as a platform/handle link. API snapshots add
  dated public fields and identify retained facts when a refresh fails.
- Web reads use PostgreSQL only. Social clients are constructed exclusively by the background
  worker. Requests use fixed HTTPS API origins, bounded bodies and no redirects. No posts, emails,
  contacts, private content or raw provider payloads are retained.

## Verify

Run the `test_social_profile_api.py` and `test_social_profile_enrichment.py` unit suites,
`test_social_profile_snapshots.py` in the isolated PostgreSQL integration runner, and the consumer
release-profile browser suite. API snapshots must keep their provider, stable ID, `fetched_at` and
facts after a new event import and a failed refresh. Disable the adapter flag to stop new requests;
keep the compatible schema and saved facts for an application rollback.
