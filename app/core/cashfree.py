from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import settings

logger = logging.getLogger("mediafetch.cashfree")


def configured() -> bool:
    # Payment gateway is explicitly opt-in; manual UPI approval stays the default.
    return bool(
        getattr(settings, "cashfree_enabled", False)
        and settings.cashfree_app_id.strip()
        and settings.cashfree_secret_key.strip()
    )


def api_base_url() -> str:
    return (
        "https://sandbox.cashfree.com/pg"
        if settings.cashfree_environment.strip().lower() != "production"
        else "https://api.cashfree.com/pg"
    )


def public_base_url() -> str:
    if settings.public_base_url.strip():
        return settings.public_base_url.strip().rstrip("/")
    if settings.antideploy_public_url.strip():
        return settings.antideploy_public_url.strip().rstrip("/")
    if settings.koyeb_public_domain.strip():
        return "https://" + settings.koyeb_public_domain.strip().rstrip("/")
    return ""


def verify_webhook_signature(raw_body: bytes, timestamp: str, signature: str) -> bool:
    secret = settings.cashfree_secret_key.strip()
    if not secret or not timestamp or not signature:
        return False
    message = timestamp.encode("utf-8") + raw_body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature.strip())


def _headers(idempotency_key: str | None = None) -> dict[str, str]:
    headers = {
        "x-client-id": settings.cashfree_app_id.strip(),
        "x-client-secret": settings.cashfree_secret_key.strip(),
        "x-api-version": settings.cashfree_api_version.strip() or "2025-01-01",
        "accept": "application/json",
        "content-type": "application/json",
    }
    if idempotency_key:
        headers["x-idempotency-key"] = idempotency_key
    return headers


async def create_order(
    *,
    order_id: str,
    amount: int,
    user_id: int,
    phone: str,
    plan: str,
) -> dict[str, Any]:
    if not configured():
        raise RuntimeError("Cashfree is not enabled or configured.")
    base_url = public_base_url()
    if not base_url:
        raise RuntimeError("Set PUBLIC_BASE_URL or KOYEB_PUBLIC_DOMAIN to enable Cashfree checkout.")
    payload: dict[str, Any] = {
        "order_id": order_id,
        "order_amount": float(amount),
        "order_currency": "INR",
        "customer_details": {
            "customer_id": f"mf_{int(user_id)}",
            "customer_phone": phone,
        },
        "order_note": f"MediaFetch {plan.title()} premium",
        "order_meta": {
            "return_url": f"{base_url}/cashfree/return?order_id={quote(order_id)}",
        },
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"{api_base_url()}/orders",
            headers=_headers(secrets.token_hex(16)),
            json=payload,
        )
    if response.status_code >= 400:
        logger.warning("Cashfree create order failed status=%s", response.status_code)
        raise RuntimeError("Cashfree could not create the payment. Please retry in a moment.")
    data = response.json()
    if not data.get("payment_session_id"):
        logger.error("Cashfree create order response missing payment_session_id")
        raise RuntimeError("Cashfree returned an incomplete checkout response.")
    return data


async def fetch_order(order_id: str) -> dict[str, Any]:
    if not configured():
        raise RuntimeError("Cashfree is not enabled or configured.")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{api_base_url()}/orders/{quote(order_id, safe='')}",
            headers=_headers(),
        )
    if response.status_code >= 400:
        logger.warning("Cashfree order lookup failed status=%s", response.status_code)
        raise RuntimeError("Could not verify the Cashfree order status.")
    return response.json()


def checkout_html(payment_session_id: str) -> str:
    import json

    session_json = json.dumps(payment_session_id)
    mode_json = json.dumps(
        "sandbox" if settings.cashfree_environment.strip().lower() != "production" else "production"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MediaFetch Secure Checkout</title>
<script src="https://sdk.cashfree.com/js/v3/cashfree.js"></script>
<style>
body{{font-family:system-ui,sans-serif;background:#0b1020;color:#f4f7ff;display:grid;min-height:100vh;place-items:center;margin:0}}
main{{max-width:420px;margin:20px;padding:28px;border:1px solid #28324a;border-radius:20px;background:#121a2e;text-align:center}}
button{{background:#7c5cff;color:white;border:0;border-radius:12px;padding:14px 22px;font-weight:700;font-size:16px}}
p{{color:#bdc7dd;line-height:1.5}}
</style></head>
<body><main><h1>⚡ MediaFetch</h1><h2>Secure Premium Checkout</h2>
<p>Payment is handled by Cashfree. MediaFetch activates your plan only after server-side payment verification.</p>
<button id="pay">Continue to payment</button><p id="status" role="status"></p></main>
<script>
const cashfree = Cashfree({{mode: {mode_json}}});
document.getElementById("pay").addEventListener("click", async () => {{
  const status = document.getElementById("status");
  status.textContent = "Opening secure checkout…";
  try {{
    await cashfree.checkout({{paymentSessionId: {session_json}, redirectTarget: "_self"}});
  }} catch (e) {{
    status.textContent = "Checkout could not open. Return to Telegram and try again.";
  }}
}});
</script></body></html>"""
