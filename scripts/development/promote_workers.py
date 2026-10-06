"""Promote the release profile's ready workers from the private development API pod."""

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from temporalio.api.enums.v1 import TaskQueueType, WorkerVersioningMode
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import (
    DescribeTaskQueueRequest,
    DescribeWorkerDeploymentRequest,
    SetWorkerDeploymentCurrentVersionRequest,
)

from events_concierge.config import get_settings
from events_concierge.runtime import load_runtime_ports
from events_concierge.workflows.temporal_client import connect_temporal, validate_temporal_settings

MAX_POLLER_AGE_SECONDS = 120


async def main(*, profile="development"):
    s = get_settings()
    if profile == "private":
        permitted = (
            s.env == "staging"
            and not s.mock_cloud
            and s.release_profile == "discovery"
            and s.database_connection_mode == "direct_tls"
            and s.identity_platform_enabled
            and not s.oidc_bff_enabled
            and s.temporal_tls_enabled
            and s.temporal_target
            == "ec-dev-temporal-frontend.events-concierge-dev.svc.cluster.local:7233"
            and s.temporal_tls_domain
            == "ec-dev-temporal-frontend.events-concierge-dev.svc.cluster.local"
            and s.runtime_provider_factory
            == "events_concierge.deployment.gcp_runtime:build_runtime_ports"
            and all(
                (
                    s.temporal_tls_server_ca_file,
                    s.temporal_tls_client_cert_file,
                    s.temporal_tls_client_key_file,
                )
            )
        )
    elif profile == "development":
        permitted = s.env == "development" and s.mock_cloud
    else:
        raise SystemExit("Unknown deployment profile")
    if not permitted or (
        s.temporal_namespace != "events-development" or not s.temporal_worker_versioning_enabled
    ):
        raise SystemExit(
            "Only versioned workers in the selected development/private profile and namespace may be promoted"
        )
    validate_temporal_settings(s)
    roles = ("catalog",) if s.release_profile == "discovery" else ("transactional", "catalog")
    ports = load_runtime_ports(s)
    client = await connect_temporal(
        s, ports.object_store, catalog_only=s.release_profile == "discovery"
    )
    rpc_timeout = timedelta(seconds=s.temporal_rpc_timeout_seconds)
    candidates = []
    # Check every enabled role before changing routing for any of them. A registered deployment
    # alone can be stale; both workflow and activity pollers must advertise this exact build.
    for role in roles:
        name = s.temporal_worker_deployment_name + "-" + role
        queue = s.temporal_catalog_queue if role == "catalog" else s.temporal_transactional_queue
        description = await client.workflow_service.describe_worker_deployment(
            DescribeWorkerDeploymentRequest(namespace=s.temporal_namespace, deployment_name=name),
            timeout=rpc_timeout,
        )
        for kind in (
            TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
            TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
        ):
            # DEFAULT returns all loaded versioned pollers; the SDK's default-build selection
            # warning applies to ENHANCED. Keep filtering deployment_options per poller.
            # https://github.com/temporalio/temporal/blob/v1.31.2/service/matching/task_queue_partition_manager.go#L821
            state = await client.workflow_service.describe_task_queue(
                DescribeTaskQueueRequest(
                    namespace=s.temporal_namespace,
                    task_queue=TaskQueue(name=queue),
                    task_queue_type=kind,
                ),
                timeout=rpc_timeout,
            )
            now = datetime.now(UTC).timestamp()
            if not any(
                poller.deployment_options.deployment_name == name
                and poller.deployment_options.build_id == s.temporal_effective_worker_build_id
                and poller.deployment_options.worker_versioning_mode
                == WorkerVersioningMode.WORKER_VERSIONING_MODE_VERSIONED
                and 0
                <= now - poller.last_access_time.ToDatetime(tzinfo=UTC).timestamp()
                <= MAX_POLLER_AGE_SECONDS
                for poller in state.pollers
            ):
                raise SystemExit(
                    f"Candidate {name} has no recent versioned {kind} poller on {queue}"
                )
        candidates.append((name, description.conflict_token))
    for name, conflict_token in candidates:
        await client.workflow_service.set_worker_deployment_current_version(
            SetWorkerDeploymentCurrentVersionRequest(
                namespace=s.temporal_namespace,
                deployment_name=name,
                build_id=s.temporal_effective_worker_build_id,
                conflict_token=conflict_token,
                identity="ec-" + profile + "-operator",
            ),
            timeout=rpc_timeout,
        )
        print("Promoted", name, s.temporal_effective_worker_build_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("development", "private"), default="development")
    asyncio.run(main(profile=parser.parse_args().profile))
