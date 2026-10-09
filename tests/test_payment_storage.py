import unittest

from app.core.storage import Storage


class PaymentStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Storage()
        # Keep these tests isolated from any configured external MongoDB.
        self.store._db = None
        self.store._payments = {}

    def _create(self, payment_id: str = "PAYMENT1", utr: str = "UTR123456") -> dict:
        return self.store.create_payment(
            payment_id=payment_id,
            user_id=123456789,
            plan="bronze",
            amount=29,
            currency="INR",
            utr=utr,
            duration_days=7,
            screenshot_file_id="telegram-photo-file-id",
            user_name="Test User",
            username="test_user",
        )

    def test_payment_persists_screenshot_and_user_details(self) -> None:
        doc = self._create()
        self.assertEqual(doc["screenshot_file_id"], "telegram-photo-file-id")
        self.assertEqual(doc["user_name"], "Test User")
        self.assertEqual(doc["username"], "test_user")
        self.assertEqual(doc["status"], "pending")

    def test_duplicate_utr_is_rejected(self) -> None:
        self._create()
        with self.assertRaisesRegex(ValueError, "UTR already submitted"):
            self._create(payment_id="PAYMENT2")

    def test_payment_can_only_be_approved_once(self) -> None:
        self._create()
        approved = self.store.approve_payment("PAYMENT1", verified_by=987654321, days=7)
        self.assertEqual(approved["status"], "approved")
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            self.store.approve_payment("PAYMENT1", verified_by=987654321, days=7)

    def test_rejected_payment_cannot_be_approved(self) -> None:
        self._create()
        rejected = self.store.reject_payment("PAYMENT1", verified_by=987654321)
        self.assertEqual(rejected["status"], "rejected")
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            self.store.approve_payment("PAYMENT1", verified_by=987654321, days=7)


if __name__ == "__main__":
    unittest.main()
