"""Compatibility exports for the former SF-specific CivicEngage RSS adapter.

The closed implementation now lives in :mod:`events_concierge.adapters.civic_engage.source` so
additional reviewed CivicEngage publishers cannot widen the San Francisco profile.
"""

from __future__ import annotations

from ..civic_engage.source import CivicEngageRssCatalogFetcher, CivicEngageRssFetchError

SfRecParkRssCatalogFetcher = CivicEngageRssCatalogFetcher
SfRecParkRssFetchError = CivicEngageRssFetchError
