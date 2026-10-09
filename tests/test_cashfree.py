import base64
import hashlib
import hmac

from app.core import cashfree
from app.core.config import settings
from app.core.storage import Storage


def test_cashfree_webhook_signature_verification(monkeypatch):
    monkeypatch.setattr(settings, "cashfree_secret_key", "unit-test-secret")
    raw = b'{"type":"PAYMENT_SUCCESS_WEBHOOK","data":{"order":{"order_id":"mf-test"}}}'
    timestamp = "1728460800"
    digest = hmac.new(b"unit-test-secret", timestamp.encode() + raw, hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode("ascii")

    assert cashfree.verify_webhook_signature(raw, timestamp, signature)
    assert not cashfree.verify_webhook_signature(raw + b" ", timestamp, signature)
    assert not cashfree.verify_webhook_signature(raw, timestamp, "invalid")


def test_cashfree_is_disabled_by_default_even_if_keys_exist(monkeypatch):
    monkeypatch.setattr(settings, "cashfree_enabled", False)
    monkeypatch.setattr(settings, "cashfree_app_id", "test-app")
    monkeypatch.setattr(settings, "cashfree_secret_key", "test-secret")
    assert not cashfree.configured()


def test_cashfree_requires_opt_in_and_both_keys(monkeypatch):
    monkeypatch.setattr(settings, "cashfree_enabled", True)
    monkeypatch.setattr(settings, "cashfree_app_id", "test-app")
    monkeypatch.setattr(settings, "cashfree_secret_key", "")
    assert not cashfree.configured()
    monkeypatch.setattr(settings, "cashfree_secret_key", "test-secret")
    assert cashfree.configured()


def test_gateway_payment_is_not_in_manual_approval_queue():
    store = Storage()
    payment = store.create_payment(
        payment_id="CASHFREE123",
        user_id=123456,
        plan="bronze",
        amount=29,
        currency="INR",
        utr="CF-mf123",
        duration_days=7,
        provider="cashfree",
        gateway_order_id="mf123",
        payment_session_id="session-test",
    )
    assert payment["provider"] == "cashfree"
    assert store.payment_by_gateway_order("mf123")["payment_id"] == "CASHFREE123"
    assert all(item["payment_id"] != "CASHFREE123" for item in store.pending_payments())
    approved = store.approve_payment("CASHFREE123", 0, 7)
    assert approved["status"] == "approved"
    assert store.plan_info(123456)["active"]
