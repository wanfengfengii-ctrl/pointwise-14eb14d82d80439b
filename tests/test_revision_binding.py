import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from lake import Lake
from lake.samples import seed, LINK, EXPECTED_CHAIN, EXPECTED_OTHER


class TestRevisionBinding(unittest.TestCase):
    def test_reopen_name_replacement_new_file_and_fixed_jobs(self):
        with TemporaryDirectory(prefix="lake-binding-") as directory:
            path = Path(directory) / "archive.sqlite"
            with Lake(path) as lake:
                ids = seed(lake)
                historical = lake.read_result("history")
                lake.create_job("frozen", ["old"], ids["reader"])
                lake.append("later", ids["old"], [{"value": 9, "next": {"branch": 0, "value": None}}])
                lake.create_job("latest", ["later"], ids["latest_reader"])
                changed = copy.deepcopy(LINK)
                changed["fields"][-1]["default"] = "third"
                latest_head = lake.register_schema("view", changed)
                self.assertEqual(lake.catalog.head("view"), latest_head)
                frozen = lake.run_job("frozen")
                current = lake.run_job("latest")
                self.assertEqual(frozen["rows"], [EXPECTED_CHAIN, EXPECTED_CHAIN])
                self.assertEqual(current["rows"], [
                    {"value": 9, "next": None, "score": 7, "label": "new"}])
                self.assertEqual(historical["rows"], [
                    EXPECTED_CHAIN, EXPECTED_CHAIN, EXPECTED_OTHER, EXPECTED_CHAIN, EXPECTED_CHAIN])
            with Lake(path) as lake:
                self.assertEqual(lake.read_result("history"), historical)
                self.assertEqual(lake.run_job("frozen"), frozen)
                self.assertEqual(lake.run_job("latest"), current)
                self.assertEqual(lake.run_job("pending")["rows"],
                                 [EXPECTED_OTHER, EXPECTED_CHAIN, EXPECTED_CHAIN])
                self.assertEqual(lake.catalog.head("view"), latest_head)
                self.assertEqual(lake.read_result("frozen")["reader_revision"], ids["reader"])
                self.assertEqual(lake.append("old", ids["old"],
                    lake.read_file("old")["records"]), ids["old_receipt"])
