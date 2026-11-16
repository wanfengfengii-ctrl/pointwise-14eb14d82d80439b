from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from lake import Lake, FailOnce, DataError
from .support import MemoryCase, SIMPLE


class TestPublication(MemoryCase):
    def test_before_commit_rolls_back_status_rows_and_receipt(self):
        revision = self.lake.register_schema("r", SIMPLE)
        self.lake.append("f", revision, [{"id": 4}, {"id": 4}])
        self.lake.create_job("j", ["f"], revision)
        before = self.lake.read_result("j")
        fault = FailOnce("publish.before_commit")
        self.lake.db.failpoint = fault
        with self.assertRaises(OSError):
            self.lake.run_job("j")
        self.assertTrue(fault.fired)
        self.assertEqual(self.lake.read_result("j"), before)
        self.assertEqual(self.lake.db.conn.execute("SELECT count(*) FROM results").fetchone()[0], 0)
        result = self.lake.run_job("j")
        self.assertEqual(result["rows"], [{"id": 4}, {"id": 4}])
        self.assertEqual(self.lake.run_job("j"), result)

    def test_corrupt_writer_payload_is_storage_error_not_resolution_failure(self):
        revision = self.lake.register_schema("r", SIMPLE)
        self.lake.append("f", revision, [{"id": 1}])
        self.lake.create_job("j", ["f"], revision)
        before = self.lake.read_result("j")
        conn = self.lake.db.conn
        payload = conn.execute("SELECT payload FROM file_rows").fetchone()[0]
        with self.lake.db.transaction("damage"):
            conn.execute("UPDATE file_rows SET payload=?", (b"invalid",))
        with self.assertRaises(DataError):
            self.lake.run_job("j")
        self.assertEqual(self.lake.read_result("j"), before)
        with self.lake.db.transaction("repair"):
            conn.execute("UPDATE file_rows SET payload=?", (payload,))
        self.assertEqual(self.lake.run_job("j")["rows"], [{"id": 1}])


class TestDiskPublication(unittest.TestCase):
    def test_reopen_after_failure_then_publish_and_reopen(self):
        with TemporaryDirectory(prefix="lake-publication-") as directory:
            path = Path(directory) / "archive.sqlite"
            with Lake(path) as lake:
                revision = lake.register_schema("r", SIMPLE)
                file_receipt = lake.append("f", revision, [{"id": 6}, {"id": 6}])
                lake.create_job("j", ["f", "f"], revision)
                queued = lake.read_result("j")
                fault = FailOnce("publish.before_commit")
                lake.db.failpoint = fault
                with self.assertRaises(OSError):
                    lake.run_job("j")
                self.assertTrue(fault.fired)
                self.assertEqual(lake.read_result("j"), queued)
            with Lake(path) as lake:
                self.assertEqual(lake.read_result("j"), queued)
                self.assertEqual(lake.append("f", revision, [{"id": 6}, {"id": 6}]), file_receipt)
                completed = lake.run_job("j")
                self.assertEqual(completed["rows"], [{"id": 6}] * 4)
                self.assertEqual(len(completed["row_ids"]), 4)
            with Lake(path) as lake:
                self.assertEqual(lake.read_result("j"), completed)
                self.assertEqual(lake.run_job("j"), completed)
                self.assertEqual(lake.db.conn.execute("SELECT count(*) FROM results").fetchone()[0], 4)
