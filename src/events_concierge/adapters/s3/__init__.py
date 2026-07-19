"""S3-compatible production object storage."""

from .object_store import S3CompatibleObjectStore, S3ObjectClient

__all__ = ["S3CompatibleObjectStore", "S3ObjectClient"]
