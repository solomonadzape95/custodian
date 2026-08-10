"""Tests for digest rendering and delivery routing.

The risk here is not a crash, it is a message going somewhere it should not.
So the tests that matter most are the ones asserting that nothing is sent:
console is the default transport, and `--send` alone is not enough.
"""

from __future__ import annotations

import dataclasses

import pytest

from custodian.config import CONFIG
from custodian.notify import Digest, deliver, render_digest


def use_transport(monkeypatch, transport: str) -> None:
    """Swap in a config with a different transport.

    Config is a frozen dataclass on purpose — settings should not be mutable at
    runtime — so the whole object is replaced rather than a field patched.
    """
    monkeypatch.setattr(
        "custodian.notify.CONFIG",
        dataclasses.replace(CONFIG, notify_transport=transport),
    )

URN_A = "urn:li:dataset:(urn:li:dataPlatform:snowflake,db.schema.orders,PROD)"
URN_B = "urn:li:dataset:(urn:li:dataPlatform:dbt,db.schema.order_items,PROD)"


class TestRenderDigest:
    def test_subject_counts_assets(self):
        one = render_digest("a@example.com", [URN_A], "staged")
        two = render_digest("a@example.com", [URN_A, URN_B], "staged")
        assert "1 asset you own" in one.subject
        assert "2 assets you own" in two.subject

    def test_body_links_back_to_datahub(self):
        digest = render_digest("a@example.com", [URN_A], "staged")
        # The artifact lives in the graph; the mail exists to drive people to
        # it. A digest without a link breaks that whole premise.
        assert "http://localhost:9002/dataset/" in digest.body

    def test_body_names_assets_readably(self):
        digest = render_digest("a@example.com", [URN_A], "staged")
        assert "db.schema.orders" in digest.body

    def test_staged_digest_says_nothing_was_deprecated(self):
        # Someone reading this must not think a deletion has already happened.
        body = render_digest("a@example.com", [URN_A], "staged").body
        assert "Nothing has been deprecated" in body
        assert "custodian approve" in body

    def test_incident_digest_carries_the_summary(self):
        digest = render_digest(
            "a@example.com", [URN_A], "incident", summary="Revenue columns are zero."
        )
        assert "Revenue columns are zero." in digest.body

    def test_unknown_kind_is_rejected(self):
        with pytest.raises(ValueError):
            render_digest("a@example.com", [URN_A], "nonsense")


class TestDeliveryRouting:
    """Sending is the irreversible act. It must take deliberate configuration."""

    ONE = [Digest(recipient="a@example.com", subject="s", body="b")]

    def test_default_transport_renders_only(self, monkeypatch, capsys):
        use_transport(monkeypatch, "console")
        assert "Rendered only" in deliver(self.ONE, send=False)

    def test_send_flag_alone_does_not_send(self, monkeypatch, capsys):
        # The dangerous combination: someone passes --send without configuring
        # SMTP. It must render and say so, not fail open into delivery.
        use_transport(monkeypatch, "console")
        result = deliver(self.ONE, send=True)
        assert "Rendered only" in result
        assert "Nothing was delivered" in result

    def test_smtp_configured_but_no_send_flag_does_not_send(self, monkeypatch):
        # Both conditions are required, in both directions.
        use_transport(monkeypatch, "smtp")
        assert "Rendered only" in deliver(self.ONE, send=False)

    def test_smtp_without_credentials_refuses_rather_than_guessing(self, monkeypatch):
        use_transport(monkeypatch, "smtp")
        monkeypatch.delenv("SMTP_HOST", raising=False)
        monkeypatch.delenv("SMTP_FROM", raising=False)
        with pytest.raises(RuntimeError, match="SMTP_HOST"):
            deliver(self.ONE, send=True)

    def test_no_digests_is_not_an_error(self):
        assert "No owners" in deliver([], send=True)
