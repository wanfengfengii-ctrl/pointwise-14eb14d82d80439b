from lake import Conflict, DataError, NotFound, FailOnce
from lake.samples import NODE, CHAIN
from .support import MemoryCase, SIMPLE


class TestArchive(MemoryCase):
    def test_order_duplicates_and_replay(self):
        revision = self.lake.register_schema("source", SIMPLE)
        records = [{"id": 2}, {"id": 1}, {"id": 2}]
        receipt = self.lake.append("f", revision, records)
        self.assertEqual(receipt, self.lake.append("f", revision, records))
        self.assertEqual(self.lake.read_file("f")["records"], records)
        self.assertEqual(receipt["row_count"], 3)
        self.assertEqual(self.lake.db.conn.execute("SELECT count(*) FROM files").fetchone()[0], 1)
        with self.assertRaises(Conflict):
            self.lake.append("f", revision, [{"id": 3}])
        other = self.lake.register_schema("source", SIMPLE)
        with self.assertRaises(Conflict):
            self.lake.append("f", other, records)

    def test_bad_final_record_leaves_no_file_or_receipt(self):
        revision = self.lake.register_schema("source", NODE)
        bad = {"value": 8, "next": {"branch": 7, "value": None}}
        with self.assertRaises(DataError):
            self.lake.append("bad", revision, [CHAIN, bad])
        with self.assertRaises(NotFound):
            self.lake.read_file("bad")
        self.assertEqual(self.lake.db.conn.execute("SELECT count(*) FROM file_rows").fetchone()[0], 0)
        self.assertEqual(self.lake.db.conn.execute("SELECT count(*) FROM files").fetchone()[0], 0)

    def test_failure_before_archive_commit_is_retryable(self):
        revision = self.lake.register_schema("source", SIMPLE)
        fault = FailOnce("archive.before_commit")
        self.lake.db.failpoint = fault
        with self.assertRaises(OSError):
            self.lake.append("f", revision, [{"id": 1}])
        self.assertTrue(fault.fired)
        with self.assertRaises(NotFound):
            self.lake.read_file("f")
        receipt = self.lake.append("f", revision, [{"id": 1}])
        self.assertEqual(receipt["row_count"], 1)

    def test_empty_file_and_unknown_revision(self):
        revision = self.lake.register_schema("source", SIMPLE)
        self.assertEqual(self.lake.append("empty", revision, [])["row_count"], 0)
        self.assertEqual(self.lake.read_file("empty")["records"], [])
        with self.assertRaises(NotFound):
            self.lake.append("unknown", 1234, [])
