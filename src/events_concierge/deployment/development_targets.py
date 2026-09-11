"""Fixed destinations for private development operations; never infer one from gcloud defaults."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DevelopmentTarget:
    project: str
    cluster: str
    payload_bucket: str
    backup_bucket: str
    terraform_root: str

    @property
    def context(self) -> str:
        return f"gke_{self.project}_us-west1-a_{self.cluster}"


TARGETS = {
    "legacy": DevelopmentTarget(
        "project-9c8cce04-f94d-40fc-aa6",
        "ec-dev",
        "iz27-ec-dev-payloads",
        "iz27-ec-dev-backups",
        "development",
    ),
    "shared": DevelopmentTarget(
        "iz27-platform-dev",
        "platform-dev",
        "iz27-platform-dev-ec-payloads",
        "iz27-platform-dev-ec-backups",
        "shared-development",
    ),
}
