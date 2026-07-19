"""Deterministic, offline pseudo-embedding standing in for the hosted embedding service behind
EmbeddingPort (FR-4.2). Hash-bucketed bag-of-tokens, L2-normalized -- no network, fully reproducible."""

from __future__ import annotations

import hashlib
import re

import numpy as np

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class DeterministicEmbedding:
    """Implements EmbeddingPort with a stable hashing-trick embedding.

    Each text is lowercase-tokenized; every token is hashed (blake2b) into a bucket in [0, dim) and
    its count accumulated, then the vector is L2-normalized. Identical text yields an identical
    vector, and texts sharing tokens land closer in cosine space -- adequate for offline ranking
    tests without any external embedding API.
    """

    def __init__(self, dim: int = 384) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self._dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = np.zeros(self._dim, dtype=np.float64)
        for token in _TOKEN_RE.findall(text.lower()):
            vector[self._bucket(token)] += 1.0
        norm = float(np.linalg.norm(vector))
        if norm > 0.0:
            vector /= norm
        return vector.tolist()

    def _bucket(self, token: str) -> int:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % self._dim
