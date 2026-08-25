"""Razorpay payment integration.

Amounts are declared server-side and must never be trusted from the browser.
Signature verification is HMAC-SHA256 over `order_id|payment_id` with KEY_SECRET.
Webhook verification is HMAC-SHA256 over the raw request body with WEBHOOK_SECRET.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from datetime import datetime, timezone

import razorpay


CURRENCY = "INR"
PRICES_PAISE = {"palm": 9900, "kundli": 9900, "bundle": 15900}
PRODUCT_TITLES = {"palm": "AI Palm Reading", "kundli": "AI Kundli", "bundle": "Palm + Kundli Bundle"}
FEATURE_GRANTS = {"palm": {"palm"}, "kundli": {"kundli"}, "bundle": {"palm", "kundli"}}


class PaymentConfigurationError(Exception):
    """Raised when Razorpay credentials are not configured."""


class PaymentVerificationError(Exception):
    """Raised when a Razorpay signature does not verify."""


def is_valid_product(product: str) -> bool:
    return product in PRICES_PAISE


class PaymentService:
    def __init__(self):
        self.key_id = os.environ.get("RAZORPAY_KEY_ID", "").strip()
        self.key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()
        self.webhook_secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.key_id and self.key_secret)

    @property
    def test_mode(self) -> bool:
        return self.key_id.startswith("rzp_test_") if self.key_id else True

    def _client(self):
        if not self.configured:
            raise PaymentConfigurationError(
                "Razorpay credentials are not configured. Ask the server administrator to set "
                "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in the backend environment."
            )
        client = razorpay.Client(auth=(self.key_id, self.key_secret))
        client.set_app_details({"title": "AstroAI", "version": "1.0"})
        return client

    def create_order(self, product: str, user_id: str):
        if not is_valid_product(product):
            raise ValueError("Unknown product")
        amount = PRICES_PAISE[product]
        client = self._client()
        # Receipt must be <= 40 chars per Razorpay contract.
        receipt = f"astroai_{product}_{uuid.uuid4().hex[:16]}"
        razor_order = client.order.create(
            {"amount": amount, "currency": CURRENCY, "receipt": receipt, "payment_capture": 1,
             "notes": {"user_id": user_id, "product": product}}
        )
        return {
            "razorpay_order_id": razor_order["id"],
            "amount": amount,
            "currency": CURRENCY,
            "product": product,
            "product_title": PRODUCT_TITLES[product],
            "key_id": self.key_id,
            "receipt": receipt,
            "razorpay_status": razor_order.get("status", "created"),
        }

    def verify_checkout_signature(self, razorpay_order_id: str, razorpay_payment_id: str, razorpay_signature: str) -> bool:
        if not (razorpay_order_id and razorpay_payment_id and razorpay_signature):
            return False
        secret = self.key_secret.encode()
        message = f"{razorpay_order_id}|{razorpay_payment_id}".encode()
        expected = hmac.new(secret, message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, razorpay_signature)

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        if not (self.webhook_secret and signature and raw_body):
            return False
        expected = hmac.new(self.webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def granted_features_for_product(product: str) -> set[str]:
    return FEATURE_GRANTS.get(product, set())


def truncate_for_preview(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    slice_ = text[:limit].rstrip()
    return slice_ + "…"
