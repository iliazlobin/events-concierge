"""Run through kubectl exec in the private API pod after both worker deployments are ready."""

import asyncio

from temporalio.api.workflowservice.v1 import (
    DescribeWorkerDeploymentRequest,
    SetWorkerDeploymentCurrentVersionRequest,
)
from temporalio.client import Client

from events_concierge.config import get_settings


async def main():
    s = get_settings()
    if s.env != "development" or not s.mock_cloud or s.temporal_namespace != "events-development":
        raise SystemExit("Only the explicit development namespace may be promoted here")
    client = await Client.connect(s.temporal_target, namespace=s.temporal_namespace)
    for role in ["transactional", "catalog"]:
        name = s.temporal_worker_deployment_name + "-" + role
        description = await client.workflow_service.describe_worker_deployment(
            DescribeWorkerDeploymentRequest(namespace=s.temporal_namespace, deployment_name=name)
        )
        await client.workflow_service.set_worker_deployment_current_version(
            SetWorkerDeploymentCurrentVersionRequest(
                namespace=s.temporal_namespace,
                deployment_name=name,
                build_id=s.temporal_effective_worker_build_id,
                conflict_token=description.conflict_token,
                identity="ec-development-operator",
            )
        )
        print("Promoted", name, s.temporal_effective_worker_build_id)


asyncio.run(main())
