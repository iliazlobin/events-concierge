# Maintenance

Containment and credential rotation. Use the [support guide](support.md) to diagnose first, then record operational authorization and prior values. Preserve data and recovery evidence.

## Incident controls

Use authorized control-plane/owner credentials; record ticket/prior values. Replace tenant placeholder only after audited lookup.

```sql
BEGIN;
SELECT public.fn_set_policy_global_kill_switch(true);
COMMIT;
BEGIN;
SELECT public.fn_set_tenant_policy_kill_switch('00000000-0000-0000-0000-000000000000'::uuid, true);
COMMIT;
SELECT public.fn_quarantine_source('meetup', 'ban');
```

1. Contain narrowly; global switch if scope unknown. Failed propagation: stop faulty worker/egress. Verify durable control and pre-mutation denial; preserve leases/outboxes/histories/objects/logs.
2. Restore dependencies → workers → API; guarded reclaim only, no forced acknowledgement.
3. Prove safe read + guarded canary before releasing tenant/global switch with `false`; verify convergence/invariants and no duplicate effects.

Quarantine release requires owner/legal review through `fn_set_source_policy`, preserving other fields. Never evade blocks with rotated accounts/proxies. [Policy controls](../../decisions/adr-004-data-plane-policy-killswitch.md).

## Secrets and key rotation

- Secret manager/workload identity; per-process mounts/IAM; no secrets in images, history, telemetry, prompts, argv or bundles. Value and `*_FILE` are mutually exclusive; files bounded, absolute, regular UTF-8.
- Create replacement → roll every consumer → verify access/queues/provider canary → revoke old credential. Compromise: revoke affected OAuth/source sessions and audit exposure.
- Local AES-GCM scaffolding is not production protection. KMS rewrap without shell plaintext; retain immutable old keys until ciphertext/backup recovery is verified.

### Application database role rotation

`EC_APP_ROLE_PASSWORD(_FILE)` bootstraps fixed non-owner `ec_app`; rerunning migrations does not rotate an existing password. **No dual-password overlap.**

1. Retain prior password/DSN; create matching numeric secret versions for `EC_APP_ROLE_PASSWORD` and `EC_DATABASE_URL`.
2. Quiesce API/all DB workers. Set `global.releasePhase=role-rotation`, `jobs.roleRotation.enabled=true`; owner files enter only the maintenance Job.
3. Run `python -m events_concierge.operations rotate-app-role-password`; require `ec_app`, login enabled, no elevated attributes/memberships. Failure/timeout blocks rollout.
4. Deploy `global.releasePhase=application` with new DSN; verify readiness/canary. Retain old secrets through observation.
5. Rollback: gated Job restores prior password **before** prior DSN/images. Helm values alone cannot restore authentication.

Redact server/proxy/audit SQL and bind parameters during rotation; client parameter hiding is insufficient.
