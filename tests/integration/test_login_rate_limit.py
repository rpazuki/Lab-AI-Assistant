"""Login throttling, which ADR-0016 promised and the code did not do.

A password endpoint with no rate limit is an offline-strength hash defending an
online-speed attack. Argon2id makes each guess expensive for the *server* too, so an
unthrottled login is also a denial-of-service surface.

Two buckets, both store-backed: one per account, one per client origin. Keys are
digests — a rate limiter is not a reason to write plaintext emails into a table
operators read during incidents.
"""

from __future__ import annotations

import pytest

from lab_assistant.application.accounts import AuthenticationFailed, LoginRateLimited
from lab_assistant.application.rate_limit import login_budget_key
from lab_assistant.testing import build_harness

PASSWORD = "correct horse battery staple"


@pytest.fixture
async def accounts():
    harness = build_harness(settings_overrides={"login_rate_limit_per_minute": 3})
    await harness.accounts.create_user(email="steward@lab.invalid", password=PASSWORD)
    return harness


class TestLoginThrottling:
    async def test_repeated_failures_start_returning_the_throttling_error(self, accounts) -> None:
        for _ in range(3):
            with pytest.raises(AuthenticationFailed):
                await accounts.accounts.authenticate(
                    email="steward@lab.invalid", password="wrong"
                )
        with pytest.raises(LoginRateLimited) as raised:
            await accounts.accounts.authenticate(email="steward@lab.invalid", password="wrong")
        assert raised.value.http_status == 429
        assert raised.value.context["retry_after_seconds"] >= 1

    async def test_the_throttling_error_says_nothing_about_the_account(self, accounts) -> None:
        """An unknown address and a known one throttle identically."""
        for _ in range(3):
            with pytest.raises(AuthenticationFailed):
                await accounts.accounts.authenticate(email="nobody@lab.invalid", password="wrong")
        with pytest.raises(LoginRateLimited) as raised:
            await accounts.accounts.authenticate(email="nobody@lab.invalid", password="wrong")
        assert "email" not in str(raised.value).lower()
        assert "nobody@lab.invalid" not in str(raised.value)

    async def test_the_bucket_is_keyed_by_digest_not_by_the_address(self, accounts) -> None:
        with pytest.raises(AuthenticationFailed):
            await accounts.accounts.authenticate(email="steward@lab.invalid", password="wrong")
        async with accounts.context.uow_factory() as uow:
            budget = await uow.tasks.get_rate_budget(login_budget_key("steward@lab.invalid"))
        assert budget is not None
        assert "steward@lab.invalid" not in budget.key
        assert budget.tokens < budget.burst

    async def test_a_successful_sign_in_clears_the_account_bucket(self, accounts) -> None:
        """Two typos then a correct password must not lock someone out."""
        for _ in range(2):
            with pytest.raises(AuthenticationFailed):
                await accounts.accounts.authenticate(
                    email="steward@lab.invalid", password="wrong"
                )
        user = await accounts.accounts.authenticate(
            email="steward@lab.invalid", password=PASSWORD
        )
        assert user.last_login_at is not None
        async with accounts.context.uow_factory() as uow:
            budget = await uow.tasks.get_rate_budget(login_budget_key("steward@lab.invalid"))
        assert budget is not None and budget.tokens == budget.burst

    async def test_one_origin_cannot_spray_many_accounts(self, accounts) -> None:
        """The per-origin bucket is what makes account enumeration expensive."""
        for index in range(3):
            with pytest.raises(AuthenticationFailed):
                await accounts.accounts.authenticate(
                    email=f"victim{index}@lab.invalid",
                    password="wrong",
                    client_signal="203.0.113.7",
                )
        # A fourth account, same origin: the per-account buckets are all fresh, so
        # only the origin bucket can stop this.
        with pytest.raises(LoginRateLimited):
            await accounts.accounts.authenticate(
                email="victim9@lab.invalid", password="wrong", client_signal="203.0.113.7"
            )

    async def test_a_different_origin_is_unaffected(self, accounts) -> None:
        for index in range(3):
            with pytest.raises(AuthenticationFailed):
                await accounts.accounts.authenticate(
                    email=f"victim{index}@lab.invalid",
                    password="wrong",
                    client_signal="203.0.113.7",
                )
        # Throttling one origin must not lock the building out.
        with pytest.raises(AuthenticationFailed):
            await accounts.accounts.authenticate(
                email="someone@lab.invalid", password="wrong", client_signal="198.51.100.4"
            )

    async def test_failures_are_still_audited_without_plaintext(self, accounts) -> None:
        with pytest.raises(AuthenticationFailed):
            await accounts.accounts.authenticate(email="steward@lab.invalid", password="wrong")
        events = [e for e in accounts.db.audit_events if e.action.value == "login_failed"]
        assert events
        rendered = repr(events[-1].metadata)
        assert "steward@lab.invalid" not in rendered
        assert "wrong" not in rendered
