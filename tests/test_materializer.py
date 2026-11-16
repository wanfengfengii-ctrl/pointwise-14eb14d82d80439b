from lake import ResolutionError, Conflict, NotFound, FailOnce
from lake.samples import ENUM_WRITER, ENUM_STRICT, ENUM_FALLBACK, union
from .support import MemoryCase, SIMPLE


class TestMaterializer(MemoryCase):
    def test_field_alias_missing_default_explicit_null_and_promotion(self):
        old = {"type": "record", "name": "R", "fields": [{"name": "old", "type": "int"}]}
        newer = {"type": "record", "name": "R", "fields": [
            {"name": "old", "type": "int"}, {"name": "score", "type": ["null", "int"]}]}
        reader = {"type": "record", "name": "View", "aliases": ["R"], "fields": [
            {"name": "id", "aliases": ["old"], "type": "long"},
            {"name": "score", "type": ["long", "null"], "default": 7}]}
        a = self.lake.register_schema("source", old)
        b = self.lake.register_schema("source", newer)
        r = self.lake.register_schema("view", reader)
        self.lake.append("a", a, [{"old": 1}])
        self.lake.append("b", b, [{"old": 2, "score": union(0, None)}])
        self.lake.create_job("j", ["a", "b"], r)
        self.assertEqual(self.lake.run_job("j")["rows"],
                         [{"id": 1, "score": 7}, {"id": 2, "score": None}])

    def test_late_enum_error_and_default_counterpart(self):
        w = self.lake.register_schema("source", ENUM_WRITER)
        strict = self.lake.register_schema("strict", ENUM_STRICT)
        fallback = self.lake.register_schema("fallback", ENUM_FALLBACK)
        self.lake.append("f", w, [{"code": "ok"}, {"code": "late"}])
        self.lake.create_job("bad", ["f"], strict)
        with self.assertRaisesRegex(ResolutionError, "row 1"):
            self.lake.run_job("bad")
        failed = self.lake.read_result("bad")
        self.assertEqual((failed["status"], failed["rows"], failed["row_ids"]), ("failed", [], []))
        self.assertIsNotNone(failed["receipt"])
        with self.assertRaises(ResolutionError):
            self.lake.run_job("bad")
        self.assertEqual(self.lake.read_result("bad"), failed)
        self.lake.create_job("good", ["f"], fallback)
        self.assertEqual(self.lake.run_job("good")["rows"], [{"code": "ok"}, {"code": "ok"}])

    def test_explicit_null_does_not_use_default(self):
        w = self.lake.register_schema("w", ["null", "int"])
        r = self.lake.register_schema("r", "long")
        self.lake.append("f", w, [union(0, None)])
        self.lake.create_job("j", ["f"], r)
        with self.assertRaises(ResolutionError):
            self.lake.run_job("j")
        self.assertEqual(self.lake.read_result("j")["rows"], [])

    def test_job_binding_conflicts_empty_and_duplicate_inputs(self):
        revision = self.lake.register_schema("r", SIMPLE)
        self.lake.append("f", revision, [{"id": 1}, {"id": 1}])
        self.lake.create_job("j", ["f", "f"], revision)
        queued = self.lake.read_result("j")
        self.assertEqual((queued["status"], queued["rows"], queued["receipt"]), ("queued", [], None))
        result = self.lake.run_job("j")
        self.assertEqual(result["rows"], [{"id": 1}] * 4)
        self.assertEqual(result["row_ids"], [["f", 0, 0], ["f", 0, 1], ["f", 1, 0], ["f", 1, 1]])
        self.assertEqual(self.lake.run_job("j"), result)
        self.assertEqual(self.lake.create_job("j", ["f", "f"], revision)["status"], "succeeded")
        with self.assertRaises(Conflict):
            self.lake.create_job("j", ["f"], revision)
        with self.assertRaises(NotFound):
            self.lake.create_job("unknown", ["missing"], revision)
        self.lake.create_job("empty", [], revision)
        self.assertEqual(self.lake.run_job("empty")["rows"], [])
        with self.assertRaises(NotFound):
            self.lake.read_result("absent")

    def test_job_commit_failure_leaves_no_binding(self):
        revision = self.lake.register_schema("r", SIMPLE)
        self.lake.append("f", revision, [{"id": 1}])
        self.lake.db.failpoint = FailOnce("job.before_commit")
        with self.assertRaises(OSError):
            self.lake.create_job("j", ["f"], revision)
        with self.assertRaises(NotFound):
            self.lake.read_result("j")
        self.lake.create_job("j", ["f"], revision)
        self.assertEqual(self.lake.run_job("j")["rows"], [{"id": 1}])
