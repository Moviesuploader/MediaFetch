import unittest

from app.bot.mtproto import split_part_count, split_part_name
from app.core.storage import Storage


class PlanSystemTests(unittest.TestCase):
    def test_tier_limits(self):
        storage = Storage()
        limits = storage.file_limits()
        self.assertEqual(limits["free"], 100)
        self.assertEqual(limits["bronze"], 500)
        self.assertEqual(limits["platinum"], 1024)
        self.assertEqual(limits["diamond"], 2048)
        self.assertEqual(limits["admin"], 0)

    def test_legacy_premium_alias_grants_bronze(self):
        storage = Storage()
        storage.set_premium(99112233, 1)
        info = storage.plan_info(99112233)
        self.assertEqual(info["plan"], "bronze")
        self.assertTrue(info["active"])

    def test_split_math_and_names(self):
        self.assertEqual(split_part_count(4 * 2000 * 1024 * 1024, 2000 * 1024 * 1024), 4)
        from pathlib import Path
        self.assertEqual(split_part_name(Path("movie.mkv"), 1, 2), "movie.part01.mkv")
        self.assertEqual(split_part_name(Path("movie.mkv"), 12, 12), "movie.part12.mkv")


if __name__ == "__main__":
    unittest.main()
