#!/bin/sh
# Explicit operator apply: CI resources only; never reads credential values.
set -eu
if [ "$#" -ne 1 ]; then
  echo 'usage: deploy/ci/install.sh --prepare | us-west1-docker.pkg.dev/iz27-platform-dev/ec-dev/ci-runner@sha256:<digest>' >&2
  exit 2
fi
ci_image=$1
if [ "$ci_image" != --prepare ]; then
python3 - "$ci_image" <<'PY'
import re, sys
if not re.fullmatch(r'us-west1-docker\.pkg\.dev/iz27-platform-dev/ec-dev/ci-runner@sha256:[0-9a-f]{64}', sys.argv[1]):
    raise SystemExit('A reviewed private CI image digest is required.')
PY
fi
ci_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# A fork can edit workflow routing. Check GitHub-side approval before touching GKE.
# Use existing operator gh authentication only; it is never delivered to job Pods.
if ! ci_approval_policy=$(gh api --hostname github.com \
  repos/iliazlobin/events-concierge/actions/permissions/fork-pr-contributor-approval \
  --jq '.approval_policy'); then
  echo 'Cannot verify external-contributor workflow approval; CI install stopped.' >&2
  exit 1
fi
if [ "$ci_approval_policy" != all_external_contributors ]; then
  echo 'Require all_external_contributors workflow approval before CI install.' >&2
  exit 1
fi
expected_context=gke_iz27-platform-dev_us-west1-a_platform-dev
test "$(kubectl config current-context)" = "$expected_context"
test "$(kubectl -n default get service kubernetes -o jsonpath='{.spec.clusterIP}')" = 10.48.0.1
test "$(kubectl -n kube-system get service kube-dns -o jsonpath='{.spec.clusterIP}')" = 10.48.0.10
umask 077
ci_charts=$(mktemp -d)
trap 'rm -rf "$ci_charts"' EXIT HUP INT TERM
ci_version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["arc_version"])' "$ci_dir/versions.json")
for ci_chart in gha-runner-scale-set-controller gha-runner-scale-set; do
  helm pull "oci://ghcr.io/actions/actions-runner-controller-charts/$ci_chart" \
    --version "$ci_version" --destination "$ci_charts"
  python3 - "$ci_dir/versions.json" "$ci_chart" "$ci_charts/$ci_chart-$ci_version.tgz" <<'PY'
import hashlib, json, pathlib, sys
expected = json.load(open(sys.argv[1]))['charts'][sys.argv[2]]['archive_sha256']
if hashlib.sha256(pathlib.Path(sys.argv[3]).read_bytes()).hexdigest() != expected:
    raise SystemExit('ARC chart archive does not match reviewed checksum.')
PY
done
# Foundation owns the cluster-wide CRDs. Never install or upgrade them per repo.
helm show crds "$ci_charts/gha-runner-scale-set-controller-$ci_version.tgz" > "$ci_charts/crds.yaml"
kubectl create --dry-run=client --validate=false -f "$ci_charts/crds.yaml" -o json > "$ci_charts/expected-crds.json"
kubectl get -f "$ci_charts/crds.yaml" -o json > "$ci_charts/live-crds.json"
python3 "$ci_dir/verify_arc_crds.py" "$ci_charts/expected-crds.json" "$ci_charts/live-crds.json"
kubectl apply -f "$ci_dir/foundation.yaml"
if [ "$ci_image" = --prepare ]; then
  echo 'Events Concierge CI namespaces prepared; deliver the approved App secret before installation.'
  exit 0
fi
test "$(kubectl -n events-concierge-ci-runners get secret events-concierge-ci-github-app -o jsonpath='{.metadata.name}')" = events-concierge-ci-github-app
helm upgrade --install events-concierge-ci-controller \
  "$ci_charts/gha-runner-scale-set-controller-$ci_version.tgz" \
  --namespace events-concierge-ci-system --values "$ci_dir/controller-values.yaml" \
  --post-renderer "$ci_dir/controller-post-renderer.sh" \
  --post-renderer-args events-concierge-ci-controller --post-renderer-args events-concierge-ci-system \
  --skip-crds --wait --atomic --timeout 5m
helm upgrade --install events-concierge-linux \
  "$ci_charts/gha-runner-scale-set-$ci_version.tgz" \
  --namespace events-concierge-ci-runners --values "$ci_dir/runner-values.yaml" \
  --set-string "template.spec.initContainers[0].image=$ci_image" \
  --set-string "template.spec.containers[0].image=$ci_image" \
  --skip-crds --wait --atomic --timeout 5m
kubectl -n events-concierge-ci-system get deployments,pods
kubectl -n events-concierge-ci-runners get autoscalingrunnersets,pods
