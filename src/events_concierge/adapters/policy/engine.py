"""In-process deterministic policy decision point (PDP).

Policy is DATA, not code (FR-10.1): behavior flips when a SourcePolicy is edited, not on deploy.
The pre-mutate guard (ADR-004) evaluates a PolicyDecision immediately before every mutating wire call,
and every evaluation fails CLOSED -- an unknown source, a quarantined policy, a disallowed modality, or
an engaged kill switch (FR-7.2) all deny. This is the foundation PDP; the production one adds a Postgres
store with LISTEN/NOTIFY hot-reload, but exposes the same PolicyEngine Protocol.
"""

from __future__ import annotations

from uuid import UUID

from ...domain.enums import Source
from ...domain.policy import (
    PolicyDecision,
    PolicyDecisionCode,
    PolicyLimits,
    PolicySnapshot,
    SourcePolicy,
)
from ...ports.policy import PolicyContext, PolicySnapshotReader


class DataPolicyEngine:
    """Implements the PolicyEngine Protocol against an in-memory declarative policy set.

    Deterministic and side-effect free per evaluation; ``set_kill_switch``/``quarantine`` are sync
    ops hooks for tests and operators (the real PDP mutates the backing store instead).
    """

    def __init__(
        self,
        *,
        source_policies: dict[Source, SourcePolicy],
        limits: PolicyLimits | None = None,
        kill_switch: bool = False,
    ) -> None:
        self._source_policies = source_policies
        self._limits = limits or PolicyLimits()
        self._kill_switch = kill_switch

    async def evaluate(self, ctx: PolicyContext) -> PolicyDecision:
        """Allow or deny at the action-moving boundary (fail CLOSED on any doubt).

        Order: kill switch -> unknown source -> quarantine/modality -> paid-surface gate (register).
        """
        if self._kill_switch:
            return PolicyDecision.deny(
                "kill switch engaged: data plane frozen (FR-7.2)",
                PolicyDecisionCode.KILL_SWITCH,
            )

        policy = self._source_policies.get(ctx.source)
        if policy is None:
            return PolicyDecision.deny(
                f"unknown source '{ctx.source.value}': no policy entry",
                PolicyDecisionCode.UNKNOWN_SOURCE,
            )

        if policy.quarantined:
            return PolicyDecision.deny(
                f"source '{ctx.source.value}' quarantined",
                PolicyDecisionCode.SOURCE_QUARANTINED,
            )

        if not policy.allows(ctx.modality):
            return PolicyDecision.deny(
                f"automation not allowed for {ctx.source.value}/{ctx.modality.value}",
                PolicyDecisionCode.MODALITY_DISABLED,
            )

        # Paid-surface gate for registration. Hook for future paid tiers; assume free at launch.
        if ctx.action == "register" and self._is_paid_surface(policy) and not policy.paid_allowed:
            return PolicyDecision.deny(
                f"paid surface '{ctx.source.value}' registration requires paid_allowed",
                PolicyDecisionCode.PAID_NOT_ALLOWED,
            )

        return PolicyDecision.allow()

    async def kill_switch_engaged(self, tenant_id: UUID | None = None) -> bool:
        """Global data-plane freeze (FR-7.2). Per-tenant scoping is a hook for the store-backed PDP."""
        return self._kill_switch

    # -- ops/test helpers (sync) -------------------------------------------------------------

    def set_kill_switch(self, engaged: bool) -> None:
        """Engage or release the global kill switch."""
        self._kill_switch = engaged

    def quarantine(self, source: Source) -> None:
        """Quarantine a source's policy in place; unknown sources are ignored (already deny)."""
        policy = self._source_policies.get(source)
        if policy is not None:
            policy.quarantined = True

    @staticmethod
    def _is_paid_surface(policy: SourcePolicy) -> bool:
        """Whether registering on this surface costs money.

        Hook only: at launch every surface is treated as free, so this returns False and the paid
        gate stays inert. A future ticketed-source policy flips this (e.g. off a paid-tier flag)
        without touching ``evaluate``.
        """
        return False


class StoreBackedPolicyEngine:
    """Evaluate a freshly read durable policy snapshot and fail closed on reader failure.

    PostgreSQL is the policy control plane, but the decision remains deterministic and in-process:
    no LLM, sidecar, cache default, or workflow signal can allow a post-freeze source mutation.
    The current implementation deliberately reads fresh on every guard, which is stronger than
    ADR-004's permitted two-second cache propagation bound.  A future listener cache may preserve
    this port contract only if a stale or unavailable cache continues to deny (FR-5.9, FR-7.2,
    FR-10.3, ADR-004).
    """

    def __init__(
        self,
        snapshots: PolicySnapshotReader,
        *,
        forced_kill_switch: bool = False,
    ) -> None:
        self._snapshots = snapshots
        self._forced_kill_switch = forced_kill_switch

    async def evaluate(self, ctx: PolicyContext) -> PolicyDecision:
        """Authoritatively allow or deny one action-moving policy check (ADR-004)."""
        if self._forced_kill_switch:
            return PolicyDecision.deny(
                "kill switch engaged: configuration data plane frozen",
                PolicyDecisionCode.KILL_SWITCH,
            )
        try:
            snapshot = await self._snapshots.read_snapshot(ctx.tenant_id, ctx.source)
        except Exception:
            return PolicyDecision.deny(
                "policy store unavailable: fail closed to handoff",
                PolicyDecisionCode.POLICY_STORE_UNAVAILABLE,
            )

        return self._evaluate_snapshot(ctx, snapshot)

    @staticmethod
    def _evaluate_snapshot(ctx: PolicyContext, snapshot: PolicySnapshot) -> PolicyDecision:
        """Apply the closed policy order to one successfully read durable snapshot."""
        reason: str | None = None
        code: PolicyDecisionCode | None = None
        policy = snapshot.source_policy
        if snapshot.control.engaged:
            reason = "kill switch engaged: data plane frozen (FR-7.2)"
            code = PolicyDecisionCode.KILL_SWITCH
        elif policy is None:
            reason = f"unknown source '{ctx.source.value}': no policy entry"
            code = PolicyDecisionCode.UNKNOWN_SOURCE
        elif policy.quarantined:
            reason = f"source '{ctx.source.value}' quarantined"
            code = PolicyDecisionCode.SOURCE_QUARANTINED
        elif not policy.allows(ctx.modality):
            reason = f"automation not allowed for {ctx.source.value}/{ctx.modality.value}"
            code = PolicyDecisionCode.MODALITY_DISABLED
        elif (
            ctx.action == "register"
            and StoreBackedPolicyEngine._is_paid_surface(policy)
            and not policy.paid_allowed
        ):
            reason = f"paid surface '{ctx.source.value}' registration requires paid_allowed"
            code = PolicyDecisionCode.PAID_NOT_ALLOWED

        if reason is None:
            return PolicyDecision.allow()
        if code is None:
            raise RuntimeError("denied policy decision is missing a closed reason code")
        return PolicyDecision.deny(reason, code)

    async def kill_switch_engaged(self, tenant_id: UUID | None = None) -> bool:
        """Read the durable data-plane freeze and treat reader failure as engaged (FR-7.2)."""
        if self._forced_kill_switch:
            return True
        try:
            control = await self._snapshots.read_control(tenant_id)
        except Exception:
            return True
        return control.engaged

    @staticmethod
    def _is_paid_surface(policy: SourcePolicy) -> bool:
        """Retain the launch-free semantics while keeping the paid-policy seam explicit."""
        del policy
        return False
