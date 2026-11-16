import copy
from lake import SchemaError, NotFound, FailOnce
from .support import MemoryCase, SIMPLE


class TestSchemaCatalog(MemoryCase):
    def test_revision_and_head_are_distinct(self):
        first = self.lake.register_schema("source", SIMPLE)
        changed = copy.deepcopy(SIMPLE)
        changed["fields"].append({"name": "note", "type": "string", "default": "old"})
        second = self.lake.register_schema("source", changed)
        self.assertNotEqual(first, second)
        self.assertEqual(self.lake.catalog.head("source"), second)
        self.assertEqual(self.lake.catalog.get(first), SIMPLE)
        changed["fields"].clear()
        self.assertEqual(len(self.lake.catalog.get(second)["fields"]), 2)

    def test_invalid_registration_preserves_head(self):
        revision = self.lake.register_schema("source", SIMPLE)
        bad = [
            {"type": "record", "name": "Bad", "fields": [{"name": "x", "type": "Missing"}]},
            {"type": "record", "name": "Bad", "fields": [
                {"name": "x", "type": {"type": "enum", "name": "Bad", "symbols": ["a"]}}]},
            {"type": "record", "name": "Bad", "fields": [
                {"name": "x", "type": "int", "default": None}]},
            {"type": "record", "name": "Bad", "fields": [
                {"name": "x", "type": "int"}, {"name": "y", "aliases": ["x"], "type": "int"}]},
        ]
        for schema in bad:
            with self.subTest(schema=schema), self.assertRaises(SchemaError):
                self.lake.register_schema("source", schema)
        self.assertEqual(self.lake.catalog.head("source"), revision)
        self.assertEqual(self.lake.db.conn.execute("SELECT count(*) FROM schema_revisions").fetchone()[0], 1)

    def test_unknown_revision_and_head(self):
        with self.assertRaises(NotFound):
            self.lake.catalog.get(99)
        with self.assertRaises(NotFound):
            self.lake.catalog.head("absent")

    def test_catalog_commit_failure_keeps_old_revision_and_head(self):
        revision = self.lake.register_schema("source", SIMPLE)
        self.lake.db.failpoint = FailOnce("catalog.before_commit")
        changed = copy.deepcopy(SIMPLE)
        changed["fields"][0]["type"] = "long"
        with self.assertRaises(OSError):
            self.lake.register_schema("source", changed)
        self.assertEqual(self.lake.catalog.head("source"), revision)
        self.assertEqual(self.lake.db.conn.execute("SELECT count(*) FROM schema_revisions").fetchone()[0], 1)
        newer = self.lake.register_schema("source", changed)
        self.assertNotEqual(newer, revision)
