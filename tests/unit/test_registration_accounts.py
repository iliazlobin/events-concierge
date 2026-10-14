"""Provider account preferences do not grant authentication or account authority."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from tests.unit.test_muse import published_event, service_for
from tests.unit.test_muse_api import ORIGIN, muse_app

from events_concierge.domain.muse import (
    MuseConflictError,
    RegistrationAccountSettings,
    RegistrationAccountUpdate,
    SignupBatch,
    SignupEvent,
    SignupItem,
    SignupOutcome,
    registration_email,
)

OWNER = {"Cookie": "fixture-session=owner", "Origin": ORIGIN, "X-Fixture-Csrf": "verified"}
PATH = "/v1/me/registration-accounts/muse"


def event_for(url="https://luma.com/account-test"):
    browse = published_event(url)
    return SignupEvent(
        canonical_event_id=browse.canonical_event.canonical_event_id,
        title=browse.canonical_event.title,
        start_at=browse.canonical_event.start_at,
        end_at=browse.canonical_event.end_at,
        venue_name=browse.canonical_event.venue_name,
        city=browse.canonical_event.city_norm,
        registration_url=url,
        observed_at=browse.sources[0].last_seen_at,
    )


@pytest.mark.parametrize("email", [
    "", "not-email", "a@localhost", "a@-example.test", "a@example-.test", "a@example.1",
    "a..b@example.test", ".a@example.test", "a.@example.test", "a b@example.test",
    "a@example.test\r\nAuthorization: secret", "\ta@example.test", "ü@example.test",
    "a@éxample.test", "a" * 65 + "@example.test", "a@" + "b" * 64 + ".test",
    "a@" + ".".join(["b" * 63] * 4) + ".test",
])
def test_invalid_email_selectors_are_rejected(email):
    with pytest.raises(ValidationError):
        RegistrationAccountUpdate(expected_email=email, provider_overrides={}, revision=1)
    with pytest.raises(ValidationError):
        RegistrationAccountUpdate(expected_email=None, provider_overrides={"luma": email}, revision=1)


def test_account_selector_preserves_aliases_and_has_no_app_identity_fallback():
    assert registration_email("  Builder+RSVP@EXAMPLE.TEST  ") == "Builder+RSVP@example.test"
    default = RegistrationAccountSettings()
    assert default.expected_email is None and default.revision == 0
    assert default.for_event(event_for()).expected_email is None
    settings = RegistrationAccountSettings(
        expected_email="default@example.test", provider_overrides={"meetup": "meetup@example.test"}
    )
    assert settings.for_event(event_for()).expected_email == "default@example.test"
    meetup = settings.for_event(event_for("https://www.meetup.com/example/events/123/"))
    assert meetup.provider == "meetup" and meetup.expected_email == "meetup@example.test"
    legacy = SignupItem(event=event_for("https://meetup.com/example/events/123/"), updated_at=datetime.now(UTC))
    assert legacy.registration_account.provider == "meetup"
    assert legacy.registration_account.expected_email is None
    with pytest.raises(ValidationError):
        RegistrationAccountUpdate(expected_email=None, provider_overrides={"unknown": "a@example.test"}, revision=1)
    with pytest.raises(ValidationError):
        RegistrationAccountUpdate(expected_email=None, provider_overrides={"luma": None}, revision=1)


async def test_owner_mapping_requires_session_csrf_and_exact_revision(monkeypatch):
    app, repository, tenant = muse_app(monkeypatch)
    repository.registration_account.return_value = RegistrationAccountSettings()
    body = {"expected_email": "provider@example.test", "provider_overrides": {}, "revision": 1}
    saved = RegistrationAccountSettings(**body, updated_at=datetime.now(UTC))
    repository.replace_registration_account.return_value = saved
    token, _ = await app.state.container.muse.connect(tenant)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        for headers in ({}, {"Authorization": "Bearer " + token}):
            assert (await client.get(PATH, headers=headers)).status_code == 401
            assert (await client.put(PATH, json=body, headers=headers)).status_code == 401
        assert (await client.put(PATH, json=body, headers={"Cookie": OWNER["Cookie"]})).status_code == 403
        repository.replace_registration_account.assert_not_awaited()
        initial = await client.get(PATH, headers=OWNER)
        assert initial.status_code == 200
        assert initial.json() == {
            "executor": "muse", "expected_email": None, "provider_overrides": {},
            "revision": 0, "updated_at": None,
        }
        response = await client.put(PATH, json=body, headers=OWNER)
        assert response.status_code == 200 and response.json() == saved.model_dump(mode="json")
        repository.replace_registration_account.assert_awaited_once_with(
            tenant, "muse", RegistrationAccountUpdate(**body)
        )
        assert response.headers["cache-control"] == "no-store, max-age=0"
        assert response.headers["referrer-policy"] == "no-referrer"
        repository.replace_registration_account.side_effect = MuseConflictError("registration account revision conflicts")
        assert (await client.put(PATH, json=body, headers=OWNER)).status_code == 409


@pytest.mark.parametrize("change", [
    {"expected_email": "bad-private-input"}, {"expected_email": ""}, {"revision": 0},
    {"revision": True}, {"revision": "1"}, {"executor": "other"},
    {"tenant_id": str(uuid4())}, {"updated_at": "2026-01-01"},
    {"provider_overrides": {"unknown": "a@example.test"}},
])
async def test_invalid_owner_mapping_is_redacted_before_repository_io(monkeypatch, change):
    app, repository, _ = muse_app(monkeypatch)
    body = {"expected_email": None, "provider_overrides": {}, "revision": 1} | change
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.put(PATH, json=body, headers=OWNER)
        assert response.status_code == 422
        assert response.json() == {"detail": "invalid Muse request"}
        assert response.headers["cache-control"] == "no-store, max-age=0"
        assert (await client.get("/v1/me/registration-accounts/unknown", headers=OWNER)).status_code == 422
    repository.replace_registration_account.assert_not_awaited()
    repository.registration_account.assert_not_awaited()


async def test_connector_list_read_claim_and_report_carry_only_selected_provider_identity(monkeypatch):
    app, repository, tenant = muse_app(monkeypatch)
    event, attempt = event_for(), uuid4()
    account = RegistrationAccountSettings(expected_email="provider@example.test").for_event(event)
    item = SignupItem(event=event, registration_account=account, updated_at=datetime.now(UTC))
    batch = SignupBatch(batch_id=uuid4(), request_id=uuid4(), created_at=datetime.now(UTC), items=[item])
    repository.batches.return_value = [batch]
    repository.batch.return_value = batch
    repository.claim.return_value = item.model_copy(update={"status": "in_progress", "attempt_id": attempt})
    app.state.container.muse.catalog.get_browse_event.return_value = published_event()
    # Reuse a claim snapshot: no need to alter catalog facts to test response authority.
    active = repository.claim.return_value
    repository.batch.return_value = batch.model_copy(update={"items": [active]})
    reported = active.model_copy(update={"status": "needs_input", "outcome": SignupOutcome(status="needs_input")})
    repository.report.return_value = reported
    token, _ = await app.state.container.muse.connect(tenant)
    headers = {"Authorization": "Bearer " + token}
    path = "/v1/muse/connector/batches/" + str(batch.batch_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        listed = (await client.get("/v1/muse/connector/batches", headers=headers)).json()[0]["items"][0]
        read = (await client.get(path, headers=headers)).json()["items"][0]
        claimed = (await client.post(path + f"/items/{event.canonical_event_id}/claim", headers=headers,
                                    json={"attempt_id": str(attempt)})).json()
        result = (await client.put(path + f"/items/{event.canonical_event_id}/outcome", headers=headers,
                                   json={"attempt_id": str(attempt), "outcome": {"status": "needs_input"}})).json()
    for data in (listed, read, claimed, result):
        assert data["registration_account"] == {
            "executor": "muse", "provider": "luma", "expected_email": "provider@example.test",
        }
        assert "tenant_id" not in data and "owner_email" not in data


async def test_replayed_claim_keeps_its_saved_account_after_settings_change():
    browse = published_event()
    service, repository, catalog = service_for(browse)
    event = event_for()
    account = RegistrationAccountSettings(expected_email="previous@example.test").for_event(event)
    attempt = uuid4()
    item = SignupItem(event=event, registration_account=account, updated_at=datetime.now(UTC),
                      status="in_progress", attempt_id=attempt)
    batch = SignupBatch(batch_id=uuid4(), request_id=uuid4(), created_at=datetime.now(UTC), items=[item])
    repository.batch.return_value = batch
    assert await service.claim(uuid4(), batch.batch_id, event.canonical_event_id, attempt) == item
    catalog.get_browse_event.assert_not_awaited()
    repository.registration_account.assert_not_awaited()
    repository.claim.assert_not_awaited()
