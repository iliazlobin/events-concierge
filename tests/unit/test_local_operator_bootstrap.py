import pytest

from events_concierge.deployment.local_operator_bootstrap import validate_local_bootstrap


@pytest.mark.parametrize("host", ["localhost:5433", "127.0.0.1:5433", "postgres:5432"])
def test_bootstrap_accepts_explicit_local_fixture(host: str) -> None:
    validate_local_bootstrap(
        environment="local",
        mock_cloud="true",
        migration_url=f"postgresql+psycopg://owner:fixture@{host}/ec",
    )


@pytest.mark.parametrize(
    "environment,mock_cloud,url",
    [
        ("staging", "true", "postgresql+psycopg://owner:fixture@localhost/ec"),
        ("local", "false", "postgresql+psycopg://owner:fixture@localhost/ec"),
        ("local", "true", "postgresql+psycopg://owner:fixture@db.example/ec"),
        ("local", "true", "postgresql+psycopg://owner:fixture@localhost/production"),
        ("local", "true", "postgresql+psycopg://owner:fixture@localhost/ec?host=db.example"),
    ],
)
def test_bootstrap_refuses_remote_or_ambiguous_targets(
    environment: str,
    mock_cloud: str,
    url: str,
) -> None:
    with pytest.raises(ValueError, match="explicit local mock"):
        validate_local_bootstrap(environment=environment, mock_cloud=mock_cloud, migration_url=url)
