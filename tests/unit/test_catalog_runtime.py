"""Private development workers share catalog payloads without consumer runtime authority."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from uuid import uuid4

import pytest

from events_concierge import catalog_runtime
from events_concierge.adapters.gcs import GcsBlob, GcsBucket, GcsObjectStore
from events_concierge.adapters.mock.object_store import MockFilesystemObjectStore
from events_concierge.config import Settings
from events_concierge.deployment import development_runtime, gcp_runtime
from events_concierge.ports.object_store import ObjectStoreNotFoundError

_FACTORY = "events_concierge.deployment.development_runtime:build_runtime_ports"
_EXECUTOR_URL = "postgresql+psycopg://executor_login:test-only@ec-postgres.ec-dev.svc/ec"


class _NotFoundError(Exception):
    code = 404


class _Blob:
    generation = 1

    def __init__(self, objects: dict[str, bytes], name: str) -> None:
        self.objects = objects
        self.name = name

    def upload_from_string(self, data: bytes, *, if_generation_match: int) -> None:
        assert if_generation_match == 0
        assert self.name not in self.objects
        self.objects[self.name] = data

    def download_as_bytes(self) -> bytes:
        if self.name not in self.objects:
            raise _NotFoundError
        return self.objects[self.name]

    def delete(self, *, if_generation_match: int) -> None:
        assert if_generation_match == 1
        del self.objects[self.name]


class _Bucket:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def blob(self, blob_name: str) -> GcsBlob:
        return _Blob(self.objects, blob_name)


class _Client:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.bucket_calls: list[str] = []

    def bucket(self, bucket_name: str) -> GcsBucket:
        self.bucket_calls.append(bucket_name)
        assert bucket_name == "ec-development-payloads"
        return _Bucket(self.objects)

    def list_blobs(
        self,
        bucket: GcsBucket,
        *,
        prefix: str,
        versions: bool,
        page_size: int,
    ) -> Iterable[GcsBlob]:
        raise AssertionError("payload exchange does not enumerate the bucket")


def _settings(**changes: object) -> Settings:
    settings = Settings(
        _env_file=None,
        env="development",
        mock_cloud=True,
        database_connection_mode="development_plaintext",
        ingestion_executor_database_url=_EXECUTOR_URL,
        ingestion_executor_enabled=True,
        runtime_provider_factory=_FACTORY,
        pacer_backend="redis",
        redis_url="redis://ec-redis.ec-dev.svc:6379/0",
        gcs_claim_check_bucket="ec-development-payloads",
        gcs_claim_check_prefix="events-concierge/catalog/v1",
        google_calendar_enabled=False,
    )
    return settings.model_copy(update=changes)


async def test_independent_development_containers_exchange_only_shared_catalog_payloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    objects: dict[str, bytes] = {}
    clients: list[_Client] = []
    databases: list[str] = []

    def new_client(*, project: str | None) -> _Client:
        client = _Client(objects)
        clients.append(client)
        return client

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("catalog startup must not construct a consumer provider graph")

    monkeypatch.setattr(gcp_runtime, "_default_storage_client", new_client)
    monkeypatch.setattr(gcp_runtime, "build_runtime_ports", forbidden)
    monkeypatch.setattr(development_runtime, "build_runtime_ports", forbidden)
    monkeypatch.setattr(catalog_runtime, "init_engine", lambda url, **kwargs: databases.append(url))
    sender = catalog_runtime.build_catalog_container(
        _settings(claim_check_local_root=str(tmp_path / "command-worker")),
    )
    receiver = catalog_runtime.build_catalog_container(
        _settings(claim_check_local_root=str(tmp_path / "catalog-worker")),
    )
    assert isinstance(sender.object_store, GcsObjectStore)
    assert isinstance(receiver.object_store, GcsObjectStore)
    assert sender.object_store is not receiver.object_store
    assert clients[0] is not clients[1]
    assert clients[0].bucket_calls == clients[1].bucket_calls == []
    assert databases == [_EXECUTOR_URL, _EXECUTOR_URL]
    assert not hasattr(sender, "calendar")
    assert not hasattr(sender, "vault")

    tenant = uuid4()
    key = "temporal/catalog.payload"
    await sender.object_store.put(tenant, key, b"catalog workflow input")
    assert await receiver.object_store.get(tenant, key) == b"catalog workflow input"
    assert list(objects) == [f"events-concierge/catalog/v1/{tenant}/{key}"]
    with pytest.raises(ObjectStoreNotFoundError):
        await receiver.object_store.get(uuid4(), key)
    consumer_store = GcsObjectStore(
        clients[1],
        bucket="ec-development-payloads",
        root_prefix="events-concierge/claim-check/v1",
    )
    with pytest.raises(ObjectStoreNotFoundError):
        await consumer_store.get(tenant, key)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"runtime_provider_factory": None}, "explicit development runtime factory"),
        ({"runtime_provider_factory": "other:factory"}, "explicit development runtime factory"),
        ({"ingestion_executor_enabled": False}, "explicit executor enablement"),
        ({"pacer_backend": "auto"}, "shared Redis pacing"),
        ({"pacer_backend": "memory"}, "shared Redis pacing"),
        ({"redis_url": ""}, "shared Redis service URL"),
        ({"redis_url": "redis://localhost:6380/0"}, "shared Redis service URL"),
        (
            {"gcs_claim_check_prefix": "events-concierge/claim-check/v1"},
            "separate events-concierge/catalog/",
        ),
        (
            {
                "ingestion_executor_database_url": "postgresql+psycopg://ec_app:test-only@ec-postgres/ec"
            },
            "separate non-owner PostgreSQL login",
        ),
    ],
)
def test_development_catalog_rejects_unsafe_configuration_before_storage_or_database(
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, object],
    message: str,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("invalid configuration must fail before storage or database startup")

    monkeypatch.setattr(catalog_runtime, "init_engine", forbidden)
    monkeypatch.setattr(catalog_runtime, "build_gcs_object_store", forbidden)
    with pytest.raises(ValueError, match=message):
        catalog_runtime.build_catalog_container(_settings(**changes))


def test_development_catalog_requires_a_gcs_bucket_before_client_or_database_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("missing bucket must fail before client or database startup")

    monkeypatch.setattr(catalog_runtime, "init_engine", forbidden)
    monkeypatch.setattr(gcp_runtime, "_default_storage_client", forbidden)
    with pytest.raises(gcp_runtime.GcpRuntimeConfigurationError, match="EC_GCS_CLAIM_CHECK_BUCKET"):
        catalog_runtime.build_catalog_container(_settings(gcs_claim_check_bucket=None))


async def test_local_mock_catalog_keeps_its_filesystem_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("local mock catalog does not initialize GCS")

    monkeypatch.setattr(catalog_runtime, "build_gcs_object_store", forbidden)
    monkeypatch.setattr(catalog_runtime, "init_engine", lambda *args, **kwargs: None)
    container = catalog_runtime.build_catalog_container(
        Settings(
            _env_file=None,
            env="local",
            mock_cloud=True,
            ingestion_executor_database_url=_EXECUTOR_URL,
            claim_check_local_root=str(tmp_path),
        )
    )
    assert isinstance(container.object_store, MockFilesystemObjectStore)
    tenant = uuid4()
    await container.object_store.put(tenant, "local.payload", b"local")
    assert await container.object_store.get(tenant, "local.payload") == b"local"
    assert (tmp_path / "catalog").is_dir()
