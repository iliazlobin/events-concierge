# Operator access

`/admin` uses Google IAP for entry and application RBAC for permitted actions.
Consumer sign-in never grants an operator role. [Public routing](public-access.md#admin)
and [consumer accounts](consumer-identity.md) configure those separate paths.

## Roles

| Role | Capabilities |
| --- | --- |
| Viewer | Read collection status, sources and model usage. |
| Operator | Viewer access, refresh commands and source enable/disable. |
| Reviewer | Operator access, source configuration and model budgets. |

The [API capability map](../../src/events_concierge/api/operator_auth.py) defines roles.
Unlisted operations are denied. GCP IAM controls cloud resources; Kubernetes RBAC controls
cluster operations; PostgreSQL roles restrict each process's database access.

## Configuration

[Google Cloud Parameter Manager](https://docs.cloud.google.com/secret-manager/parameter-manager/docs/overview)
stores the private role assignments. Secret Manager continues to hold credentials.
The [consumer-identity Terraform root](../../infra/terraform/environments/consumer-identity)
creates an empty JSON parameter and a GET-only runtime role, restricted by an IAM condition
to that parameter's versions. Only the operator API's Workload Identity receives it.
IAM bindings are project-level; the condition narrows their resource scope.

| Setting | Purpose |
| --- | --- |
| Terraform `operator_api_service_account` | Existing operator API identity allowed to read the policy. |
| Terraform `operator_iap_member` | Approved member admitted by the backend's separate IAP grant; supply privately. |
| Helm `operator.policyVersion` / `EC_OPERATOR_POLICY_VERSION` | Exact `projects/PROJECT_NUMBER/locations/global/parameters/ec-operator-rbac/versions/VERSION` resource. |
| Helm `operator.policyCacheSeconds` / `EC_OPERATOR_POLICY_CACHE_SECONDS` | Demand-driven cache: 30 seconds by default, at most 60. |

Policy JSON has `schema: 1` and `bindings`, each containing a verified `subject`, `email`
and `role`. Subject and signed email must both match. At most 100 bindings are accepted;
unknown fields, duplicate subjects and invalid roles fail validation. Empty bindings
deny everyone. Keep identity values in the private payload, outside Git, Helm and Terraform
state; Terraform must not manage version payloads. Explicit local fixtures may use static
assignments, but deployed operator profiles require Parameter Manager.

The [policy reader](../../src/events_concierge/adapters/operator_policy.py) uses ADC and a
fixed Google API endpoint. It validates the version and payload after authenticating
each request's signed assertion. When its cache expires, an unavailable, disabled or
invalid version blocks access; stale grants are never reused. The API reports a generic
authorization-unavailable error and fails readiness without logging identities or payloads.

## Publish and activate

1. Review and authorize the saved Terraform plan in the [identity setup](consumer-identity.md#gcp-setup).
   Keep the existing backend IAP member and restricted workload identities explicit.
2. Prepare a private, permission-restricted policy file. Verify subjects against signed
   assertions through the approved operator channel; never trust an unsigned identity header.
3. With configuration-change authorization, publish a named immutable version from that file:

```sh
gcloud parametermanager parameters versions create "$POLICY_VERSION" \
  --project=iz27-platform-dev --account="$APP_GCP_ACCOUNT" \
  --parameter=ec-operator-rbac --location=global \
  --payload-data-from-file="$POLICY_FILE" --format='value(name)'
```

4. Pin the returned version using the numeric project number in private release values;
   leave `operator.subjectRoles` empty. Review and roll out the configuration through the
   [private release procedure](release.md#gke-release). No image rebuild
   is needed for subsequent grant changes; changing the version pin requires a rollout.
5. Verify API readiness, permitted reads/writes by role, unassigned/incorrect identities,
   forged assertions and policy-outage denial. IAP membership and application RBAC must
   both allow access. Test consumer rejection separately.

For emergency denial, disable the pinned version through the authorized operator channel:

```sh
gcloud parametermanager parameters versions update "$POLICY_VERSION" \
  --project=iz27-platform-dev --account="$APP_GCP_ACCOUNT" \
  --parameter=ec-operator-rbac --location=global --disabled --format='value(name)'
```

Successful cached grants expire within the configured TTL; an in-flight operation may
finish. Disable/revoke IAP access as needed for edge sessions. Restore only an enabled,
reviewed policy whose grants are still approved; never restore revoked access as rollback.
Parameter storage and reads have [usage-based pricing](https://cloud.google.com/secret-manager/pricing#parameter-manager-pricing);
authentication and readiness checks share the cache, without a separate poller. Source and fixture tests do not prove
cloud provisioning, access grants or deployed browser acceptance.
