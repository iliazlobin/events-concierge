"""Temporal durable-execution spine: a short-lived parent EventRequest workflow owns discover/rank and
the attempt loop; per-(user, event) child Registration workflows own the saga (ADR-003)."""
