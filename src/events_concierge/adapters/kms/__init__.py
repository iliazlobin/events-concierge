"""KMS-envelope adapters for persisted secret projections."""

from .notification_secrets import KmsNotificationSecretProtector

__all__ = ["KmsNotificationSecretProtector"]

