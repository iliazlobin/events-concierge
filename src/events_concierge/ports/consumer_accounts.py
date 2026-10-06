"""Application account creation and legal acceptance, independent of the identity provider."""

from typing import Protocol
from uuid import UUID

from ..domain.consumer_identity import LegalPolicy, VerifiedConsumerIdentity


class ConsumerAccountRepository(Protocol):
    async def bootstrap(self, identity: VerifiedConsumerIdentity) -> UUID:
        """Resolve an immutable account without claiming or writing legal acceptance."""
        ...

    async def accept(self, identity: VerifiedConsumerIdentity, policy: LegalPolicy) -> UUID:
        """Atomically create or resolve one immutable account and record explicit acceptance."""
        ...

    async def has_accepted(self, tenant_id: UUID, policy: LegalPolicy) -> bool:
        """Require this account's receipt for the currently published legal documents."""
        ...

    async def is_ready(self, *, legal_required: bool = True) -> bool:
        """Check the narrow signup capability before enabling provider sign-in."""
        ...
