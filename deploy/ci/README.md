# Private Linux CI

Events Concierge has a repository-scoped Actions Runner Controller (ARC) bootstrap for
plain shell and JavaScript jobs on the shared private GKE `platform-ci` pool. The
existing [CI](../../.github/workflows/ci.yml) and
[deployment validation](../../.github/workflows/deployment-validation.yml) remain on
GitHub-hosted runners with all their checks. This package adds only a manually dispatched
[isolation smoke test](../../.github/workflows/private-ci-smoke.yml). Installing the
package, registering a runner and passing native GKE jobs are separate acceptance steps.

## Roles and boundaries

| Resource | Purpose and access |
| --- | --- |
| `events-concierge-ci-system` | One ARC controller and listener on `shared-dev`; watches only this repository's runner namespace. |
| `events-concierge-ci-runners` | `events-concierge-linux` scale set: zero idle runners, at most one assigned job. |
| Shared `platform-ci` node pool | Foundation-owned, tainted, private gVisor execution pool; may also run separately isolated Symphony CI. |
| Shared ARC CRDs | Foundation-owned, exact pinned ARC version; repository installers cannot install or change them. |
| `events-concierge-private-ci` GitHub App | Install only on `iliazlobin/events-concierge`; repository Administration read/write and Metadata read, no organization permissions. |
| `events-concierge-ci-github-app` | App ID, installation ID and private key in the runner namespace; read by ARC, never mounted or injected into jobs. |
| `ec-dev/ci-runner` image | This repository's pinned Python 3.12, uv and Node 22 image; no application source, Docker, model client or credentials. |

Runner Pods use UID/GID 1001, Tini as PID 1, a read-only image, no Linux capabilities,
no privilege escalation and no service-account token. Each job gets a new disk-backed
home (8 GiB) and temporary directory (2 GiB), removed with its Pod. No retained storage,
application namespace, host path, Docker socket, cloud identity or another repository's
App is attached. Requests are 2 CPU/6 GiB; limits are 3 CPU/10 GiB with bounded ephemeral
storage and a one-hour Pod deadline. Network policy allows cluster DNS and public IPv4
HTTPS, excludes private/link-local ranges, and denies ingress. ARC alone also reaches
the two reviewed private Kubernetes API addresses. Public HTTPS is not a domain allowlist.

Each repository has a one-Pod quota and `maxRunners: 1`. On the single shared 4-vCPU
node, allocatable CPU is less than 4 CPU, so two 2-CPU jobs cannot run together; another
assigned job can remain Pending. Kubernetes scheduling provides no FIFO guarantee.
Before activation, check live node headroom for both repositories' controller/listener
requests (150m CPU and 256 MiB per repository); do not reduce job requests or bypass quota
to force admission. Before installing either release, require at least 300m CPU and
512 MiB memory available for both pairs, subtracting only the requests of these
recognized CI control Pods when already present. Controllers use
the checked-in post-renderer to select `Recreate`, because the pinned chart has no
strategy setting and an extra rolling-update Pod would exhaust current node headroom.
Controller updates briefly pause reconciliation; existing runner jobs continue.

## Public repository admission

This repository is public. Private runner execution does not make GitHub logs or uploaded
artifacts private. Self-hosted runners can execute repository code, so keep automatic
jobs hosted until native acceptance and a separately reviewed routing change.

Maintain GitHub's `all_external_contributors` approval policy while the scale set is
registered. `install.sh` verifies it with the operator's existing `gh` authentication
before any Kubernetes or Helm change, and fails closed on an unavailable or weaker policy.
Do not approve an external workflow that targets the private runner. A fork can edit its
workflow YAML: a same-repository `runs-on` expression is not an admission boundary.
Future routing must retain hosted fallback for forks and Dependabot, and preserve every
required check. Personal repositories cannot use organization runner groups to enforce
workflow-only access. Review the default-branch and selected dispatch revision before
running the manual pilot; repository writers can select other branches.

See GitHub's [self-hosted runner security guidance](https://docs.github.com/en/actions/reference/security/secure-use),
[fork approval](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/approve-runs-from-forks)
and [ARC authentication requirements](https://docs.github.com/en/actions/how-tos/manage-runners/use-actions-runner-controller/authenticate-to-the-api).

## Prepare and install

Use the existing private-cluster access procedure in [development operations](../development.md).
The operator needs `gh`, Python 3, Helm and kubectl, the approved GitHub identity, and
context `gke_iz27-platform-dev_us-west1-a_platform-dev`. No operator auth store is copied
into an image or Pod. The foundation must first provision the shared pool and four ARC
CRDs from the checksummed 0.15.0 controller chart. Repository installation checks every
CRD's complete schema/version specification, Established and NamesAccepted conditions,
and storage version before any mutation; only Kubernetes v1 serialization defaults are
normalized. Missing, partial, deleting or incompatible CRDs stop installation. Coordinate
upgrades across both repositories through the foundation, then update each pinned package.

Create and install the dedicated App. Its planned Secret Manager home is
`projects/iz27-platform-dev/secrets/events-concierge-ci-github-app`, with JSON keys
`github_app_id`, `github_app_installation_id` and `github_app_private_key`. The payload
interface matches ARC; its App, installation and secret are exclusive to this repository.
Do not put the PEM in command arguments, source, logs or job environment variables.
App creation, secret delivery and GKE apply require authorized operator execution.
Prepare namespaces through the guarded installer before secret delivery:

```sh
deploy/ci/install.sh --prepare
```

This runs the same approval, private-context, chart and shared-CRD checks, then applies
only `foundation.yaml`. It does not read secrets or install Helm releases. Deliver the
approved payload as `events-concierge-ci-github-app` in `events-concierge-ci-runners`;
never bypass the preflights with a manual foundation apply.

Build the CI image from a clean reviewed commit. The CI-specific Docker ignore file
excludes the repository build context; only checksummed upstream tools enter the image.

```sh
ci_revision=$(git rev-parse HEAD)
test -z "$(git status --porcelain)"
docker buildx build --platform linux/amd64 \
  --build-arg "SOURCE_REVISION=$ci_revision" \
  -f deploy/ci/Dockerfile \
  -t "us-west1-docker.pkg.dev/iz27-platform-dev/ec-dev/ci-runner:$ci_revision" \
  --push .
```

Resolve the uploaded immutable digest and inspect its source/revision labels. Run the
installer with that exact reviewed digest, replacing the placeholder below:

```sh
deploy/ci/install.sh 'us-west1-docker.pkg.dev/iz27-platform-dev/ec-dev/ci-runner@sha256:<64-hex-digest>'
```

The installer verifies GitHub approval, context, API/DNS addresses, chart checksums and
shared CRDs, then applies this repository's foundation. With the prepared secret
present, it verifies the name and installs both
namespaced releases with `--skip-crds`. It never reads key values. Retain
the source commit, image digest and Helm values for rollback. Installer reapplication
does not rotate the App key, replace CRDs or change application workloads.

## Verify native execution

Dispatch **Private CI smoke** once from the reviewed default-branch revision. Its first
job checks tool versions, privilege boundaries, PID 1, read-only root, blocked Kubernetes
API/metadata access and allowed GitHub DNS/HTTPS, then writes a home sentinel. A dependent
second job checks that the sentinel and protected credentials are absent in a fresh Pod.
Verify different Pod/runner identities, old Pod deletion and zero idle runners afterward.
Capture the exact source/image digest, job results and Pod lifecycle without secret values.
Local Docker testing cannot establish gVisor, GKE network policy or ARC registration.

Bootstrap unit tests run automatically in the existing hosted static/unit job:

```sh
uv run pytest tests/unit/test_private_ci.py
sh -n deploy/ci/install.sh
sh -n deploy/ci/controller-post-renderer.sh
git diff --check
```

The image has no Docker daemon, socket, sudo, Kubernetes tools or container hooks.
Service-backed integration, container/canary, deployment and browser dependency-installation
jobs stay hosted. Only consider separately tested plain static/web jobs after successful
native smoke and full required-check validation; a passing smoke is not application
release acceptance. Stop new dispatches before maintenance, let assigned jobs finish,
and verify runner cleanup before uninstalling namespaced releases. Retain foundation
CRDs and App secret until the owning operator completes approved cleanup.
