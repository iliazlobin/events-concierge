"""In-memory NotificationPort mock (mock_cloud=True stand-in for SES).

Appends delivered Notifications to a list, deduped by dedup_key so delivery is at-most-once
user-visible even under at-least-once retry (notifications port contract)."""

from __future__ import annotations

from ...ports.notifications import Notification


class MockNotifier:
    """Process-local NotificationPort implementation. Records sent notifications; dedups on dedup_key."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []
        self._seen: set[str] = set()

    async def send(self, notification: Notification) -> None:
        """Record the notification unless its dedup_key was already delivered (at-most-once)."""
        if notification.dedup_key in self._seen:
            return
        self._seen.add(notification.dedup_key)
        self.sent.append(notification)
