import unittest

from app.core.config import settings


class UploadTransportConfigTests(unittest.TestCase):
    def test_requested_split_contract(self):
        self.assertEqual(settings.large_upload_split_mb, 2000)
        self.assertEqual(settings.local_bot_api_max_upload_mb, 2000)

    def test_plan_constants(self):
        self.assertEqual(settings.free_max_file_mb, 100)
        self.assertEqual(settings.bronze_max_file_mb, 500)
        self.assertEqual(settings.platinum_max_file_mb, 1024)
        self.assertEqual(settings.diamond_max_file_mb, 2048)
        self.assertEqual(settings.admin_max_file_mb, 0)


if __name__ == "__main__":
    unittest.main()
