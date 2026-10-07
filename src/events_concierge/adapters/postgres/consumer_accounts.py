"""The consumer signup capability writes identity and legal acceptance in one transaction."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

from ...domain.consumer_identity import LegalPolicy, VerifiedConsumerIdentity
from ...infra.db import system_session_scope, tenant_session_scope
from ...ports.auth import (
    BrowserSessionUnavailableError,
    ConsumerSignInFailureReason,
    ConsumerSignInRejectedError,
)


class PostgresConsumerAccountRepository:
    async def bootstrap(self, identity: VerifiedConsumerIdentity) -> UUID:
        try:
            async with system_session_scope() as session:
                tenant_id = (
                    await session.execute(
                        text(
                            "SELECT public.fn_bootstrap_consumer_account(:tenant_id,:subject,:email)"
                        ),
                        {
                            "tenant_id": identity.tenant_id,
                            "subject": identity.subject,
                            "email": identity.email,
                        },
                    )
                ).scalar_one()
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "42501":
                raise ConsumerSignInRejectedError(
                    ConsumerSignInFailureReason.ACCOUNT_UNAVAILABLE
                ) from error
            raise BrowserSessionUnavailableError("consumer signup unavailable") from error
        except (SqlAlchemyTimeoutError, TimeoutError) as error:
            raise BrowserSessionUnavailableError("consumer signup unavailable") from error
        if tenant_id != identity.tenant_id:
            raise BrowserSessionUnavailableError("invalid consumer account binding")
        return identity.tenant_id

    async def accept(self, identity: VerifiedConsumerIdentity, policy: LegalPolicy) -> UUID:
        try:
            async with system_session_scope() as session:
                tenant_id = (
                    await session.execute(
                        text("""
                    SELECT public.fn_accept_consumer_account(
                        :tenant_id,:subject,:email,:terms_version,:terms_url,:privacy_version,:privacy_url)
                """),
                        {
                            "tenant_id": identity.tenant_id,
                            "subject": identity.subject,
                            "email": identity.email,
                            "terms_version": policy.terms_version,
                            "terms_url": policy.terms_url,
                            "privacy_version": policy.privacy_version,
                            "privacy_url": policy.privacy_url,
                        },
                    )
                ).scalar_one()
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "42501":
                raise ConsumerSignInRejectedError(
                    ConsumerSignInFailureReason.ACCOUNT_UNAVAILABLE
                ) from error
            raise BrowserSessionUnavailableError("consumer signup unavailable") from error
        except (SqlAlchemyTimeoutError, TimeoutError) as error:
            raise BrowserSessionUnavailableError("consumer signup unavailable") from error
        if tenant_id != identity.tenant_id:
            raise BrowserSessionUnavailableError("invalid consumer account binding")
        return identity.tenant_id

    async def has_accepted(self, tenant_id: UUID, policy: LegalPolicy) -> bool:
        async with tenant_session_scope(tenant_id) as session:
            return bool(
                (
                    await session.execute(
                        text("""
                SELECT EXISTS(SELECT 1 FROM public.consumer_account_consents
                    WHERE tenant_id=:tenant_id AND terms_version=:terms_version
                    AND terms_url=:terms_url AND privacy_version=:privacy_version
                    AND privacy_url=:privacy_url)
            """),
                        {
                            "tenant_id": tenant_id,
                            "terms_version": policy.terms_version,
                            "terms_url": policy.terms_url,
                            "privacy_version": policy.privacy_version,
                            "privacy_url": policy.privacy_url,
                        },
                    )
                ).scalar_one()
            )

    async def is_ready(self, *, legal_required: bool = True) -> bool:
        try:
            async with system_session_scope() as session:
                return bool(
                    (
                        await session.execute(
                            text("""
                    SELECT coalesce(has_function_privilege(current_user,
                        to_regprocedure(:signature),
                        'EXECUTE'), false)
                """),
                            {
                                "signature": "public.fn_accept_consumer_account(uuid,text,text,text,text,text,text)"
                                if legal_required
                                else "public.fn_bootstrap_consumer_account(uuid,text,text)"
                            },
                        )
                    ).scalar_one()
                )
        except (DBAPIError, SqlAlchemyTimeoutError, TimeoutError):
            return False
