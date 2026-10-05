"""Offline boundaries for the additive, manually dispatched private CI bootstrap."""

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / "deploy/ci"
IMAGE = "us-west1-docker.pkg.dev/iz27-platform-dev/ec-dev/ci-runner@sha256:" + "a" * 64
SYSTEM = "events-concierge-ci-system"
RUNNERS = "events-concierge-ci-runners"


def load_module(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, CI / (name + ".py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CRDS = load_module("verify_arc_crds")
PROBE = load_module("probe_runner")
RENDER = load_module("render_controller")


def crd_fixture() -> dict:
    return {
        "items": [
            {
                "apiVersion": "apiextensions.k8s.io/v1",
                "kind": "CustomResourceDefinition",
                "metadata": {"name": name},
                "spec": {
                    "group": "actions.github.com",
                    "scope": "Namespaced",
                    "versions": [{"name": "v1alpha1", "served": True, "storage": True}],
                },
                "status": {
                    "conditions": [
                        {"type": name, "status": "True"}
                        for name in ("Established", "NamesAccepted")
                    ],
                    "storedVersions": ["v1alpha1"],
                },
            }
            for name in sorted(CRDS.CRD_NAMES)
        ]
    }


def test_job_identity_storage_and_capacity_stay_repository_scoped() -> None:
    runner = yaml.safe_load((CI / "runner-values.yaml").read_text())
    assert runner["githubConfigUrl"] == "https://github.com/iliazlobin/events-concierge"
    assert runner["githubConfigSecret"] == "events-concierge-ci-github-app"
    assert runner["runnerScaleSetName"] == "events-concierge-linux"
    assert (runner["minRunners"], runner["maxRunners"]) == (0, 1)
    assert "containerMode" not in runner
    pod = runner["template"]["spec"]
    assert pod["serviceAccountName"] == "events-concierge-ci-job"
    assert pod["automountServiceAccountToken"] is False
    assert pod["runtimeClassName"] == "gvisor"
    assert pod["nodeSelector"] == {"node-restriction.kubernetes.io/workload": "platform-ci"}
    assert {
        "key": "workload",
        "operator": "Equal",
        "value": "platform-ci",
        "effect": "NoSchedule",
    } in pod["tolerations"]
    assert pod["securityContext"]["runAsUser"] == 1001
    assert pod["securityContext"]["runAsGroup"] == 1001
    assert pod["securityContext"]["fsGroup"] == 1001
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["activeDeadlineSeconds"] == 3600
    assert pod["restartPolicy"] == "Never"
    assert pod["volumes"] == [
        {"name": "runner-home", "emptyDir": {"sizeLimit": "8Gi"}},
        {"name": "temporary", "emptyDir": {"sizeLimit": "2Gi"}},
    ]
    for container in pod["initContainers"] + pod["containers"]:
        assert container["securityContext"] == {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        }
        assert container["image"] == "REQUIRES_REVIEWED_CI_IMAGE_DIGEST"
        assert "env" not in container and "envFrom" not in container
    assert pod["containers"][0]["command"] == ["/usr/bin/tini", "--", "/home/runner/run.sh"]
    assert pod["containers"][0]["resources"] == {
        "requests": {"cpu": "2", "memory": "6Gi", "ephemeral-storage": "6Gi"},
        "limits": {"cpu": "3", "memory": "10Gi", "ephemeral-storage": "10Gi"},
    }
    assert "cp -R " in pod["initContainers"][0]["command"][2]


def test_foundation_never_grants_job_api_or_cross_repository_access() -> None:
    objects = list(yaml.safe_load_all((CI / "foundation.yaml").read_text()))
    assert {item["kind"] for item in objects} == {
        "Namespace",
        "ServiceAccount",
        "ResourceQuota",
        "NetworkPolicy",
    }
    assert {item["metadata"].get("namespace") for item in objects} == {None, SYSTEM, RUNNERS}
    for item in objects:
        if item["kind"] == "Namespace":
            assert item["metadata"]["name"] in (SYSTEM, RUNNERS)
            assert item["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
        elif item["kind"] == "ServiceAccount":
            assert item["automountServiceAccountToken"] is False
            assert "annotations" not in item["metadata"]
    quota = next(item["spec"]["hard"] for item in objects if item["kind"] == "ResourceQuota")
    assert quota["pods"] == "1" and quota["persistentvolumeclaims"] == "0"
    assert quota["requests.cpu"] == "3" and quota["limits.cpu"] == "4"
    policies = [item for item in objects if item["kind"] == "NetworkPolicy"]
    deny = [item for item in policies if item["metadata"]["name"] == "default-deny"]
    assert len(deny) == 2
    assert all(
        item["spec"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]} for item in deny
    )
    jobs = next(
        item["spec"] for item in policies if item["metadata"]["name"] == "dns-and-public-https"
    )
    assert jobs["podSelector"] == {"matchLabels": {"events-concierge-ci-role": "runner"}}
    assert len(jobs["egress"]) == 2
    dns, https = jobs["egress"]
    assert dns["ports"] == [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]
    assert https["ports"] == [{"protocol": "TCP", "port": 443}]
    block = https["to"][0]["ipBlock"]
    assert block["cidr"] == "0.0.0.0/0"
    assert set(block["except"]) == {
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "224.0.0.0/4",
        "240.0.0.0/4",
    }
    control = next(
        item["spec"] for item in policies if item["metadata"]["name"] == "control-egress"
    )
    assert control["egress"][1] == {
        "to": [{"ipBlock": {"cidr": "10.48.0.1/32"}}, {"ipBlock": {"cidr": "10.40.0.2/32"}}],
        "ports": [{"protocol": "TCP", "port": 443}],
    }
    controller = yaml.safe_load((CI / "controller-values.yaml").read_text())
    assert controller["flags"]["watchSingleNamespace"] == RUNNERS
    assert controller["serviceAccount"]["name"] == "events-concierge-ci-controller"
    assert controller["resources"]["requests"] == {"cpu": "100m", "memory": "128Mi"}


def test_pilot_is_manual_sequential_and_existing_jobs_remain_hosted() -> None:
    smoke = yaml.safe_load((ROOT / ".github/workflows/private-ci-smoke.yml").read_text())
    assert smoke.get("on", smoke.get(True)) == {"workflow_dispatch": None}
    assert smoke["permissions"] == {"contents": "read"}
    assert set(smoke["jobs"]) == {"isolated-runner", "clean-replacement"}
    assert smoke["jobs"]["clean-replacement"]["needs"] == "isolated-runner"
    for job in smoke["jobs"].values():
        assert job["runs-on"] == "events-concierge-linux"
        assert job["timeout-minutes"] == 10
        assert (
            job["steps"][0]["uses"] == "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"
        )
        assert job["steps"][0]["with"]["persist-credentials"] is False
    assert smoke["jobs"]["isolated-runner"]["steps"][1]["run"].endswith("--write-sentinel")
    assert (
        smoke["jobs"]["clean-replacement"]["steps"][1]["run"] == "python3 deploy/ci/probe_runner.py"
    )
    for filename in ("ci.yml", "deployment-validation.yml"):
        workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
        assert all(job["runs-on"] == "ubuntu-latest" for job in workflow["jobs"].values())


def test_image_pins_and_context_exclude_application_and_credentials() -> None:
    versions = json.loads((CI / "versions.json").read_text())
    dockerfile = (CI / "Dockerfile").read_text()
    assert "FROM " + versions["python_image"] in dockerfile
    assert "FROM " + versions["uv_image"] + " AS uv" in dockerfile
    assert versions["runner_archive_sha256"] in dockerfile
    assert versions["node_archive_sha256"] in dockerfile
    assert f"node-v{versions['node_version']}-linux-x64.tar.xz" in dockerfile
    assert 'ENTRYPOINT ["/usr/bin/tini", "--"]' in dockerfile
    assert "USER 1001:1001" in dockerfile and "SOURCE_REVISION" in dockerfile
    assert [
        line
        for line in (CI / "Dockerfile.dockerignore").read_text().splitlines()
        if line and not line.startswith("#")
    ] == ["*"]
    assert [line for line in dockerfile.splitlines() if line.startswith("COPY")] == [
        "COPY --from=uv /uv /uvx /usr/local/bin/"
    ]


def test_crd_stream_and_api_defaults_are_equivalent(tmp_path: Path) -> None:
    expected = crd_fixture()
    stream = tmp_path / "stream.json"
    stream.write_text("\n".join(json.dumps(item) for item in expected["items"]))
    assert CRDS.read_document(stream) == expected
    list_path = tmp_path / "list.json"
    list_path.write_text(json.dumps(expected))
    assert CRDS.read_document(list_path) == expected
    live = copy.deepcopy(expected)
    for item in live["items"]:
        item["spec"].update(conversion={"strategy": "None"}, preserveUnknownFields=False)
    CRDS.verify(expected, live)


@pytest.mark.parametrize(
    "failure", ["missing", "duplicate", "schema", "versions", "stored", "unhealthy", "deleting"]
)
def test_crds_reject_drift_or_incomplete_state(failure: str) -> None:
    expected = crd_fixture()
    live = copy.deepcopy(expected)
    item = live["items"][0]
    if failure == "missing":
        live["items"].pop()
    elif failure == "duplicate":
        live["items"][-1] = copy.deepcopy(item)
    elif failure == "schema":
        item["spec"]["unexpected"] = "must not be ignored"
    elif failure == "versions":
        item["spec"]["versions"][0]["name"] = "v2"
    elif failure == "stored":
        item["status"]["storedVersions"].append("v0")
    elif failure == "unhealthy":
        item["status"]["conditions"][0]["status"] = "False"
    else:
        item["metadata"]["deletionTimestamp"] = "2026-10-05T00:00:00Z"
    with pytest.raises(ValueError):
        CRDS.verify(expected, live)


@pytest.mark.parametrize(
    "data", ["", "[]", '{"items": {}}', '{"items": []}', "{} {} {} {} {}", "{} invalid"]
)
def test_crd_reader_rejects_malformed_or_extra_input(tmp_path: Path, data: str) -> None:
    fixture = tmp_path / "crds.json"
    fixture.write_text(data)
    with pytest.raises((ValueError, KeyError)):
        CRDS.definitions(CRDS.read_document(fixture))


@pytest.fixture
def installer_fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    package = tmp_path / "ci"
    package.mkdir()
    for name in (
        "install.sh",
        "verify_arc_crds.py",
        "versions.json",
        "foundation.yaml",
        "controller-values.yaml",
        "runner-values.yaml",
    ):
        (package / name).write_bytes((CI / name).read_bytes())
    versions = json.loads((package / "versions.json").read_text())
    for name, entry in versions["charts"].items():
        entry["archive_sha256"] = hashlib.sha256(name.encode()).hexdigest()
    (package / "versions.json").write_text(json.dumps(versions))
    fixture = tmp_path / "expected.json"
    fixture.write_text(json.dumps(crd_fixture()))
    log = tmp_path / "commands.jsonl"
    tool = tmp_path / "tool.py"
    tool.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,pathlib,sys\n"
        "name=pathlib.Path(sys.argv[0]).name; args=sys.argv[1:]; scenario=os.environ['SCENARIO']\n"
        "with open(os.environ['COMMAND_LOG'],'a') as out: out.write(json.dumps([name,*args])+'\\n')\n"
        "if name=='gh':\n"
        " assert 'repos/iliazlobin/events-concierge/actions/permissions/fork-pr-contributor-approval' in args\n"
        " if scenario=='approval-api-error': sys.exit(1)\n"
        " print('first_time_contributors' if scenario=='weak-approval' else 'all_external_contributors')\n"
        "elif name=='helm':\n"
        " if args[0]=='pull':\n"
        "  chart=args[1].rsplit('/',1)[-1]; version=args[args.index('--version')+1]\n"
        "  dest=pathlib.Path(args[args.index('--destination')+1]); (dest/(chart+'-'+version+'.tgz')).write_text(chart)\n"
        " elif args[:2]==['show','crds']: print('fixture')\n"
        "elif name=='kubectl':\n"
        " if args==['config','current-context']: print('wrong-context' if scenario=='wrong-context' else 'gke_iz27-platform-dev_us-west1-a_platform-dev')\n"
        " elif 'kubernetes' in args: print('10.48.0.1')\n"
        " elif 'kube-dns' in args: print('10.48.0.10')\n"
        " elif args[0]=='create' or args[:2]==['get','-f']:\n"
        "  doc=json.loads(pathlib.Path(os.environ['EXPECTED']).read_text())\n"
        "  if args[0]=='get':\n"
        "   if scenario=='missing-crds': sys.exit(1)\n"
        "   if scenario=='partial-crds': doc['items'].pop()\n"
        "   if scenario=='changed-crds': doc['items'][0]['spec']['scope']='Cluster'\n"
        "  for item in doc['items']: print(json.dumps(item))\n"
        " elif 'secret' in args: print('events-concierge-ci-github-app')\n"
    )
    tool.chmod(0o755)
    for name in ("gh", "helm", "kubectl"):
        (tmp_path / name).symlink_to(tool)
    environment = {
        "PATH": str(tmp_path)
        + os.pathsep
        + str(Path(sys.executable).parent)
        + os.pathsep
        + "/usr/bin:/bin",
        "SCENARIO": "matching",
        "COMMAND_LOG": str(log),
        "EXPECTED": str(fixture),
    }
    return package, environment, log


@pytest.mark.parametrize("mode", [IMAGE, "--prepare"])
@pytest.mark.parametrize(
    "scenario",
    [
        "weak-approval",
        "approval-api-error",
        "wrong-context",
        "missing-crds",
        "partial-crds",
        "changed-crds",
    ],
)
def test_installer_fails_before_mutation(
    installer_fixture: tuple, scenario: str, mode: str
) -> None:
    package, environment, log = installer_fixture
    environment["SCENARIO"] = scenario
    result = subprocess.run(
        ["sh", str(package / "install.sh"), mode],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any(cmd[:2] in (["kubectl", "apply"], ["helm", "upgrade"]) for cmd in commands)
    if scenario in ("weak-approval", "approval-api-error"):
        assert all(cmd[0] == "gh" for cmd in commands)


def test_matching_installer_skips_shared_crds_and_installs_only_own_releases(
    installer_fixture: tuple,
) -> None:
    package, environment, log = installer_fixture
    result = subprocess.run(
        ["sh", str(package / "install.sh"), IMAGE],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    upgrades = [cmd for cmd in commands if cmd[:2] == ["helm", "upgrade"]]
    assert len(upgrades) == 2
    assert all("--skip-crds" in cmd for cmd in upgrades)
    assert {cmd[3] for cmd in upgrades} == {
        "events-concierge-ci-controller",
        "events-concierge-linux",
    }
    assert {cmd[cmd.index("--namespace") + 1] for cmd in upgrades} == {SYSTEM, RUNNERS}
    controller = next(cmd for cmd in upgrades if cmd[3] == "events-concierge-ci-controller")
    assert controller[controller.index("--post-renderer") + 1].endswith(
        "/controller-post-renderer.sh"
    )
    assert [
        controller[index + 1]
        for index, arg in enumerate(controller)
        if arg == "--post-renderer-args"
    ] == ["events-concierge-ci-controller", SYSTEM]
    assert all(
        not (cmd[0] == "kubectl" and "secret" in cmd and "{.data" in " ".join(cmd))
        for cmd in commands
    )


def test_prepare_runs_all_preflights_and_only_applies_own_foundation(
    installer_fixture: tuple,
) -> None:
    package, environment, log = installer_fixture
    result = subprocess.run(
        ["sh", str(package / "install.sh"), "--prepare"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert commands[0][0] == "gh"
    assert any(cmd[:3] == ["kubectl", "get", "-f"] for cmd in commands)
    assert commands[-1] == ["kubectl", "apply", "-f", str(package / "foundation.yaml")]
    assert not any("secret" in cmd or cmd[:2] == ["helm", "upgrade"] for cmd in commands)


def test_controller_renderer_changes_only_exact_deployment_strategy() -> None:
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "events-concierge-ci-controller", "namespace": SYSTEM},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1}},
        },
    }
    account = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": "account", "namespace": SYSTEM},
    }
    source = [account, deployment]
    expected = copy.deepcopy(source)
    expected[1]["spec"]["strategy"] = {"type": "Recreate"}
    for serialized in (
        json.dumps({"items": source}),
        "\n".join(json.dumps(item) for item in source),
    ):
        rendered = RENDER.render(serialized, "events-concierge-ci-controller", SYSTEM)
        assert list(yaml.safe_load_all(rendered)) == expected


@pytest.mark.parametrize("failure", ["missing", "multiple", "namespace", "name", "api-version"])
def test_controller_renderer_rejects_ambiguous_target(failure: str) -> None:
    resources = [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "events-concierge-ci-controller", "namespace": SYSTEM},
            "spec": {},
        }
    ]
    if failure == "missing":
        resources.clear()
    elif failure == "multiple":
        resources.append(copy.deepcopy(resources[0]))
    elif failure == "namespace":
        resources[0]["metadata"]["namespace"] = "symphony-ci-system"
    elif failure == "name":
        resources[0]["metadata"]["name"] = "other-controller"
    else:
        resources[0]["apiVersion"] = "extensions/v1beta1"
    with pytest.raises(ValueError):
        RENDER.render(json.dumps({"items": resources}), "events-concierge-ci-controller", SYSTEM)


@pytest.mark.parametrize(
    "image",
    [
        IMAGE.replace("ec-dev/ci-runner", "symphony/ci-runner"),
        IMAGE.split("@", maxsplit=1)[0] + ":latest",
        IMAGE[:-1],
        "$(echo unsafe)",
    ],
)
def test_wrong_image_never_contacts_github_or_cluster(installer_fixture: tuple, image: str) -> None:
    package, environment, log = installer_fixture
    result = subprocess.run(
        ["sh", str(package / "install.sh"), image],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not log.exists()


@pytest.fixture
def probe_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    versions = json.loads((CI / "versions.json").read_text())
    monkeypatch.setattr(PROBE.os, "getuid", lambda: 1001)
    monkeypatch.setattr(PROBE.shutil, "which", lambda _: None)
    monkeypatch.setattr(PROBE.pathlib.Path, "exists", lambda _: False)
    original_read = Path.read_text

    def read(path: Path, *args: object, **kwargs: object) -> str:
        contents = {
            "/proc/self/status": "CapEff:\t0000000000000000\nNoNewPrivs:\t1\n",
            "/proc/self/mountinfo": "1 0 0:1 / / ro,relatime - overlay overlay ro\n",
            "/proc/1/comm": "tini\n",
        }
        return contents.get(str(path)) or original_read(path, *args, **kwargs)

    monkeypatch.setattr(PROBE.pathlib.Path, "read_text", read)
    monkeypatch.setattr(PROBE.platform, "python_version", lambda: versions["python_version"])
    outputs = {
        "node": "v" + versions["node_version"],
        "uv": "uv " + versions["uv_version"],
        "/home/runner/bin/Runner.Listener": versions["runner_version"],
    }
    monkeypatch.setattr(PROBE.subprocess, "check_output", lambda args, **_: outputs[args[0]])
    monkeypatch.setattr(
        PROBE.socket, "create_connection", Mock(side_effect=OSError("blocked fixture"))
    )
    monkeypatch.setattr(PROBE.socket, "getaddrinfo", Mock(return_value=[]))
    response = Mock()
    response.__enter__ = Mock(return_value=Mock(status=200))
    response.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(PROBE.urllib.request, "urlopen", Mock(return_value=response))
    for name in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
        "DOCKER_HOST",
        "GITHUB_APP_PRIVATE_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_probe_checks_all_protected_endpoints_and_positive_https(probe_fixture: None) -> None:
    PROBE.probe()
    assert [call.args[0] for call in PROBE.socket.create_connection.call_args_list] == [
        ("10.48.0.1", 443),
        ("10.40.0.2", 443),
        ("169.254.169.254", 80),
    ]
    PROBE.urllib.request.urlopen.assert_called_once_with("https://api.github.com/", timeout=15)


@pytest.mark.parametrize(
    "failure", ["uid", "credential", "path", "tools", "python", "private-network", "sentinel"]
)
def test_probe_rejects_unsafe_runner(
    probe_fixture: None, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if failure == "uid":
        monkeypatch.setattr(PROBE.os, "getuid", lambda: 0)
    elif failure == "credential":
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fixture-not-a-key")
    elif failure == "path":
        monkeypatch.setattr(
            PROBE.pathlib.Path, "exists", lambda path: str(path).endswith("serviceaccount/token")
        )
    elif failure == "tools":
        monkeypatch.setattr(PROBE.shutil, "which", lambda _: "/usr/bin/docker")
    elif failure == "python":
        monkeypatch.setattr(PROBE.platform, "python_version", lambda: "3.11.0")
    elif failure == "private-network":
        monkeypatch.setattr(PROBE.socket, "create_connection", Mock(return_value=Mock()))
    else:
        monkeypatch.setattr(
            PROBE.pathlib.Path, "exists", lambda path: str(path).endswith("ci-sentinel")
        )
    with pytest.raises(RuntimeError):
        PROBE.probe()


@pytest.mark.parametrize(
    "path,contents",
    [
        ("/proc/self/status", "CapEff:\t0000000000000001\nNoNewPrivs:\t1\n"),
        ("/proc/self/status", "CapEff:\t0000000000000000\nNoNewPrivs:\t0\n"),
        ("/proc/self/mountinfo", "1 0 0:1 / / rw,relatime - overlay overlay rw\n"),
        ("/proc/1/comm", "python3\n"),
    ],
)
def test_probe_rejects_privileged_or_unreaped_runtime(
    probe_fixture: None, monkeypatch: pytest.MonkeyPatch, path: str, contents: str
) -> None:
    original = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda location, *args, **kwargs: (
            contents if str(location) == path else original(location, *args, **kwargs)
        ),
    )
    with pytest.raises(RuntimeError):
        PROBE.probe()
