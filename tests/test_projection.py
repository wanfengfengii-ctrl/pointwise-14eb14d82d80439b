"""Independent accounting for finite-state projection exports.

These tests deliberately rebuild expectations from hand-written values rather
than reusing any projection helper: they inspect the published archive through
``read_file`` (tagged records) and reconcile it with the untagged projection.
"""
import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from lake import (DataError, FailOnce, Lake, NotFound,
                  ResolutionError, SchemaError)
from lake.samples import (CHAIN, ENUM_FALLBACK, ENUM_WRITER, LINK, NODE,
                          OTHER_CHAIN, REVERSED)
from projection_examples import (EXPECTED_IDS, EXPECTED_ROWS, SELECTION, seed)
from vendor import sdk1


def u(branch, value):
    return {"branch": branch, "value": value}


def state_doc(selection):
    return json.loads(json.dumps(selection, sort_keys=True))


def schema_state(lake):
    conn = lake.db.conn
    return {
        "revisions": [tuple(row) for row in conn.execute(
            "SELECT * FROM schema_revisions ORDER BY revision")],
        "heads": [tuple(row) for row in conn.execute(
            "SELECT * FROM schema_heads ORDER BY name")],
        "files": [tuple(row) for row in conn.execute(
            "SELECT * FROM files ORDER BY file_id")],
        "exports": [tuple(row) for row in conn.execute(
            "SELECT * FROM projection_exports ORDER BY export_id")],
    }


class TestTaggedResolutionAccounting(unittest.TestCase):
    """The tagged resolver must agree with the original SDK on every fixture."""

    def test_stripped_tags_match_sdk1_resolution(self):
        from lake.projection import strip_tags, tagged_resolve
        cases = [
            (NODE, LINK, [CHAIN]),
            (REVERSED, LINK, [OTHER_CHAIN]),
        ]
        for writer, reader, values in cases:
            wtree, rtree = sdk1.Schema(writer), sdk1.Schema(reader)
            for value in values:
                with self.subTest(writer=writer["name"], value=value):
                    tagged = tagged_resolve(writer, reader, value, wtree, rtree)
                    self.assertEqual(strip_tags(rtree, rtree.root, tagged),
                                     sdk1.resolve(writer, reader, value))

    def test_reader_tags_follow_reader_order_even_for_equal_shapes(self):
        from lake.projection import tagged_resolve
        a = {"type": "record", "name": "A", "fields": [{"name": "v", "type": "int"}]}
        b = {"type": "record", "name": "B", "fields": [{"name": "v", "type": "int"}]}
        writer = {"type": "record", "name": "R", "fields": [{"name": "x", "type": [a, b]}]}
        reader = {"type": "record", "name": "R", "fields": [{"name": "x", "type": [b, a]}]}
        self.assertEqual(tagged_resolve(writer, reader, {"x": u(0, {"v": 7})}),
                         {"x": u(1, {"v": 7})})
        self.assertEqual(tagged_resolve(writer, reader, {"x": u(1, {"v": 7})}),
                         {"x": u(0, {"v": 7})})


class TestPublicFixtureProjection(unittest.TestCase):
    def test_rows_schema_tags_and_identity(self):
        with Lake() as lake:
            source = seed(lake)
            before = schema_state(lake)
            meta = lake.export_projection("history", copy.deepcopy(SELECTION))
            self.assertEqual(set(meta), {"export_id", "job_id", "file_id",
                                         "writer_revision", "row_count", "receipt"})
            self.assertEqual(meta["job_id"], "history")
            self.assertEqual(meta["row_count"], 5)
            for key in ("export_id", "file_id", "receipt"):
                self.assertIsInstance(meta[key], str)
                self.assertTrue(meta[key])

            exported = lake.read_projection(meta["export_id"])
            self.assertEqual(exported["rows"], EXPECTED_ROWS)
            self.assertEqual(exported["row_ids"], EXPECTED_IDS)
            self.assertEqual(exported["schema"], lake.catalog.get(meta["writer_revision"]))
            self.assertEqual({key: exported[key] for key in meta}, meta)

            # The output schema is closed and independently decodes every row.
            tree = sdk1.Schema(exported["schema"])
            root = tree.deref(tree.root)
            self.assertEqual([f["name"] for f in root["fields"]],
                             ["value", "choice", "next", "score"])
            child = tree.deref(next(f["type"] for f in root["fields"]
                                    if f["name"] == "next")[0])
            self.assertEqual([f["name"] for f in child["fields"]],
                             ["choice", "next", "score"])

            archive = lake.read_file(meta["file_id"])
            self.assertEqual(archive["writer_revision"], meta["writer_revision"])
            self.assertEqual(archive["row_count"], 5)
            records = archive["records"]
            for record, expected in zip(records, EXPECTED_ROWS):
                payload = sdk1.encode(exported["schema"], record)
                self.assertEqual(sdk1.decode(exported["schema"], payload), record)
                self.assertEqual(sdk1.resolve(exported["schema"], exported["schema"], record),
                                 expected)
            # Reader-side union identities: writer A/B order differs from reader.
            self.assertEqual(records[0]["choice"], u(1, {}))
            self.assertEqual(records[0]["next"]["branch"], 0)
            middle = records[0]["next"]["value"]
            self.assertEqual(middle["choice"], u(0, {}))
            last = middle["next"]["value"]
            self.assertEqual((last["value"], last["choice"], last["next"]), (3, u(1, {}), u(1, None)))
            self.assertEqual(records[2]["choice"], u(0, {}))
            self.assertEqual(records[2]["score"], u(1, None))
            self.assertEqual([r["score"] for r in records],
                             [u(0, 7), u(0, 7), u(1, None), u(0, 7), u(0, 7)])

            # Source result, heads and existing files are untouched.
            self.assertEqual(lake.read_result("history"), source)
            self.assertEqual(schema_state(lake)["heads"], before["heads"])
            self.assertTrue(set(before["files"]).issubset(set(schema_state(lake)["files"])))

    def test_replay_invariance_and_input_copies(self):
        with Lake() as lake:
            seed(lake)
            request = copy.deepcopy(SELECTION)
            meta = lake.export_projection("history", request)
            snapshot = schema_state(lake)

            # Key order is irrelevant to selection identity.
            reordered = state_doc(SELECTION)
            self.assertEqual(lake.export_projection("history", reordered), meta)
            self.assertEqual(schema_state(lake), snapshot)

            # Mutating caller-owned inputs/returns cannot change the export.
            request["states"].clear()
            meta["row_count"] = -1
            exported = lake.read_projection(meta["export_id"])
            self.assertEqual(exported["rows"], EXPECTED_ROWS)
            exported["rows"].clear()
            exported["schema"]["fields"].clear()
            fresh = lake.read_projection(meta["export_id"])
            self.assertEqual(fresh["rows"], EXPECTED_ROWS)
            self.assertEqual(schema_state(lake), snapshot)

    def test_empty_file_and_empty_job_still_emit_full_schema(self):
        with Lake() as lake:
            node = lake.register_schema("node", NODE)
            lake.append("empty", node, [])
            lake.create_job("empty_file", ["empty"], node)
            lake.create_job("empty_job", [], node)
            lake.run_job("empty_file")
            lake.run_job("empty_job")
            selection = {"root": "even", "states": {
                "even": {"record": "Node", "fields": {"value": {}, "next": {"Node": "odd"}}},
                "odd": {"record": "Node", "fields": {"next": {"Node": "even"}}}}}
            for job_id in ("empty_file", "empty_job"):
                with self.subTest(job_id=job_id):
                    meta = lake.export_projection(job_id, copy.deepcopy(selection))
                    self.assertEqual(meta["row_count"], 0)
                    exported = lake.read_projection(meta["export_id"])
                    self.assertEqual(exported["rows"], [])
                    self.assertEqual(exported["row_ids"], [])
                    tree = sdk1.Schema(exported["schema"])
                    root = tree.deref(tree.root)
                    self.assertEqual([f["name"] for f in root["fields"]], ["value", "next"])
                    # Recursive closure present without any sample row.
                    child = tree.deref(next(f["type"] for f in root["fields"]
                                            if f["name"] == "next")[1])
                    self.assertEqual([f["name"] for f in child["fields"]], ["next"])
                    back = tree.deref(child["fields"][0]["type"][1])
                    self.assertEqual(back["name"], root["name"])

    def test_defaults_explicit_null_and_enum_fallback(self):
        writer = {"type": "record", "name": "Q", "fields": [
            {"name": "id", "type": "int"},
            {"name": "score", "type": ["null", "int"]}]}
        defaults = {"type": "record", "name": "Q", "fields": [
            {"name": "id", "type": "int"},
            {"name": "score", "type": ["long", "null"], "default": 7}]}
        old = {"type": "record", "name": "Q", "fields": [{"name": "id", "type": "int"}]}
        with Lake() as lake:
            a = lake.register_schema("old", old)
            b = lake.register_schema("new", writer)
            r = lake.register_schema("read", defaults)
            lake.append("a", a, [{"id": 1}])
            lake.append("b", b, [{"id": 2, "score": u(0, None)},
                                 {"id": 3, "score": u(1, 9)}])
            lake.create_job("j", ["a", "b"], r)
            self.assertEqual(lake.run_job("j")["rows"],
                             [{"id": 1, "score": 7}, {"id": 2, "score": None},
                              {"id": 3, "score": 9}])
            selection = {"root": "s", "states": {
                "s": {"record": "Q", "fields": {"score": {}}}}}
            meta = lake.export_projection("j", selection)
            exported = lake.read_projection(meta["export_id"])
            self.assertEqual(exported["rows"], [{"score": 7}, {"score": None}, {"score": 9}])
            self.assertEqual([row["score"] for row in lake.read_file(meta["file_id"])["records"]],
                             [u(0, 7), u(1, None), u(0, 9)])

        with Lake() as lake:
            w = lake.register_schema("w", ENUM_WRITER)
            fallback = lake.register_schema("f", ENUM_FALLBACK)
            lake.append("e", w, [{"code": "ok"}, {"code": "late"}])
            lake.create_job("j", ["e"], fallback)
            lake.run_job("j")
            meta = lake.export_projection("j", {"root": "s", "states": {
                "s": {"record": "Event", "fields": {"code": {}}}}})
            exported = lake.read_projection(meta["export_id"])
            self.assertEqual(exported["rows"], [{"code": "ok"}, {"code": "ok"}])
            self.assertEqual(exported["schema"]["fields"][0]["type"],
                             {"type": "enum", "name": "Code", "symbols": ["ok"]})

    def test_same_record_with_different_shapes_per_state(self):
        node = {"type": "record", "name": "Node", "fields": [
            {"name": "value", "type": "int"},
            {"name": "next", "type": ["null", "Node"]}]}
        chain = {"value": 1, "next": u(1, {"value": 2, "next": u(0, None)})}
        selection = {"root": "even", "states": {
            "even": {"record": "Node", "fields": {"value": {}, "next": {"Node": "odd"}}},
            "odd": {"record": "Node", "fields": {"next": {"Node": "even"}}}}}
        with Lake() as lake:
            rev = lake.register_schema("r", node)
            lake.append("f", rev, [chain])
            lake.create_job("j", ["f"], rev)
            lake.run_job("j")
            meta = lake.export_projection("j", selection)
            exported = lake.read_projection(meta["export_id"])
            self.assertEqual(exported["rows"],
                             [{"value": 1, "next": {"next": None}}])
            tree = sdk1.Schema(exported["schema"])
            root = tree.deref(tree.root)
            self.assertEqual([f["name"] for f in root["fields"]], ["value", "next"])
            odd = tree.deref(next(f["type"] for f in root["fields"]
                                  if f["name"] == "next")[1])
            self.assertEqual([f["name"] for f in odd["fields"]], ["next"])
            self.assertNotEqual(odd["name"], root["name"])
            back = tree.deref(odd["fields"][0]["type"][1])
            self.assertEqual(back["name"], root["name"])
            # The two shapes must not share a single named definition.
            self.assertNotEqual(tree.names[odd["name"]], tree.names[root["name"]])
            # Selection key order never controls output; identity/schema are stable.
            shuffled = {"states": {
                "odd": {"fields": {"next": {"Node": "even"}}, "record": "Node"},
                "even": {"fields": {"next": {"Node": "odd"}, "value": {}},
                         "record": "Node"}}, "root": "even"}
            again = lake.export_projection("j", shuffled)
            self.assertEqual(again["export_id"], meta["export_id"])
            self.assertEqual(lake.read_projection(again["export_id"])["schema"],
                             exported["schema"])

    def test_equal_shape_branches_keep_reader_writer_identifier(self):
        # PROJECTION.md: writer item:[A,B] but reader item:[B,A]; both A/B are
        # {v:int}.  Even with every inner field deleted, the branch must be the
        # reader branch actually selected by the original resolution.
        a = {"type": "record", "name": "A", "fields": [{"name": "v", "type": "int"}]}
        b = {"type": "record", "name": "B", "fields": [{"name": "v", "type": "int"}]}
        writer = {"type": "record", "name": "Root", "fields": [{"name": "item", "type": [a, b]}]}
        reader = {"type": "record", "name": "Root", "fields": [{"name": "item", "type": [b, a]}]}
        selection = {"root": "r", "states": {
            "r": {"record": "Root", "fields": {"item": {"A": "x", "B": "y"}}},
            "x": {"record": "A", "fields": {}},
            "y": {"record": "B", "fields": {}}}}
        with Lake() as lake:
            w = lake.register_schema("w", writer)
            r = lake.register_schema("r", reader)
            lake.append("f", w, [{"item": u(0, {"v": 7})}, {"item": u(1, {"v": 7})}])
            lake.create_job("j", ["f"], r)
            source = lake.run_job("j")
            self.assertEqual(source["rows"], [{"item": {"v": 7}}, {"item": {"v": 7}}])
            meta = lake.export_projection("j", selection)
            exported = lake.read_projection(meta["export_id"])
            self.assertEqual(exported["rows"], [{"item": {}}, {"item": {}}])
            records = lake.read_file(meta["file_id"])["records"]
            self.assertEqual([record["item"] for record in records],
                             [u(1, {}), u(0, {})])
            # Branch order in the output union is the reader's [B, A].
            root = sdk1.Schema(exported["schema"]).deref(exported["schema"])
            names = [sdk1.Schema(exported["schema"]).deref(branch)["name"]
                     for branch in root["fields"][0]["type"]]
            self.assertEqual([name.split("__")[0] for name in names], ["B", "A"])

    def test_nested_union_branches_and_field_order_preserved(self):
        a = {"type": "record", "name": "A", "fields": [{"name": "v", "type": "int"}]}
        e = {"type": "enum", "name": "E", "symbols": ["z", "y"]}
        writer = {"type": "record", "name": "Root", "fields": [
            {"name": "drop", "type": "string"},
            {"name": "x", "type": ["null", [a, "string"]]},
            {"name": "kind", "type": e}]}
        with Lake() as lake:
            rev = lake.register_schema("r", writer)
            lake.append("f", rev, [
                {"drop": "d", "x": u(1, u(0, {"v": 4})), "kind": "y"},
                {"drop": "d", "x": u(1, u(1, "text")), "kind": "z"}])
            lake.create_job("j", ["f"], rev)
            lake.run_job("j")
            selection = {"root": "s", "states": {
                "s": {"record": "Root", "fields": {"x": {"A": "t"}, "kind": {}}},
                "t": {"record": "A", "fields": {"v": {}}}}}
            meta = lake.export_projection("j", selection)
            exported = lake.read_projection(meta["export_id"])
            root = sdk1.Schema(exported["schema"]).deref(exported["schema"])
            self.assertEqual([f["name"] for f in root["fields"]], ["x", "kind"])
            self.assertEqual(len(root["fields"][0]["type"]), 2)
            self.assertIsInstance(root["fields"][0]["type"][1], list)
            self.assertEqual(root["fields"][1]["type"],
                             {"type": "enum", "name": "E", "symbols": ["z", "y"]})
            self.assertEqual(exported["rows"], [{"x": {"v": 4}, "kind": "y"},
                                                {"x": "text", "kind": "z"}])
            records = lake.read_file(meta["file_id"])["records"]
            self.assertEqual(records[0]["x"], u(1, u(0, {"v": 4})))
            self.assertEqual(records[1]["x"], u(1, u(1, "text")))


class TestSelectionValidation(unittest.TestCase):
    ROOT = {"type": "record", "name": "Root", "aliases": ["Alias"], "fields": [
        {"name": "n", "type": "int"},
        {"name": "a", "type": ["null", {"type": "record", "name": "A",
                                        "fields": [{"name": "v", "type": "int"},
                                                   {"name": "w", "type": "string"}]}]}]}

    def setUp(self):
        self.lake = Lake()
        self.addCleanup(self.lake.close)
        rev = self.lake.register_schema("r", self.ROOT)
        self.lake.append("f", rev, [{"n": 1, "a": u(1, {"v": 2, "w": "x"})}])
        self.lake.create_job("j", ["f"], rev)
        self.lake.run_job("j")
        self.before = schema_state(self.lake)

    def bad(self, selection):
        with self.assertRaises(SchemaError):
            self.lake.export_projection("j", selection)
        self.assertEqual(schema_state(self.lake), self.before)

    def test_selection_syntax_errors(self):
        valid = {"root": "s", "states": {
            "s": {"record": "Root", "fields": {"a": {"A": "t"}}},
            "t": {"record": "A", "fields": {"v": {}}}}}
        self.assertTrue(self.lake.export_projection("j", copy.deepcopy(valid)))
        self.before = schema_state(self.lake)
        broken = []
        broken.append([])
        broken.append({"root": "s"})
        mutated = copy.deepcopy(valid)
        mutated["extra"] = 1
        broken.append(mutated)
        mutated = copy.deepcopy(valid)
        mutated["states"]["s"]["x"] = 1
        broken.append(mutated)
        mutated = copy.deepcopy(valid)
        mutated["root"] = "absent"
        broken.append(mutated)
        mutated = copy.deepcopy(valid)
        mutated["states"]["s"]["record"] = "Alias"   # aliases are not accepted
        broken.append(mutated)
        mutated = copy.deepcopy(valid)
        mutated["states"]["s"]["record"] = "A"       # root binds the wrong record
        broken.append(mutated)
        mutated = copy.deepcopy(valid)
        mutated["states"]["s"]["fields"]["zz"] = {}
        broken.append(mutated)
        mutated = copy.deepcopy(valid)
        mutated["states"]["s"]["fields"]["a"] = {}   # missing the A transition
        broken.append(mutated)
        mutated = copy.deepcopy(valid)
        mutated["states"]["s"]["fields"]["n"] = {"A": "t"}  # primitive has no leaf
        broken.append(mutated)
        mutated = copy.deepcopy(valid)
        mutated["states"]["s"]["fields"]["a"] = {"A": "t", "Root": "s"}
        broken.append(mutated)
        mutated = copy.deepcopy(valid)
        mutated["states"]["t"]["record"] = "Root"    # wrong record for the leaf
        broken.append(mutated)
        mutated = copy.deepcopy(valid)
        mutated["states"]["s"]["fields"]["a"] = {"A": "missing"}
        broken.append(mutated)
        mutated = copy.deepcopy(valid)
        mutated["states"]["unused"] = {"record": "A", "fields": {"nope": {}}}
        broken.append(mutated)                       # unreachable states still count
        mutated = copy.deepcopy(valid)
        mutated["states"] = {}
        broken.append(mutated)
        for selection in broken:
            with self.subTest(selection=selection):
                self.bad(selection)

    def test_status_and_identity_errors(self):
        rev = self.lake.register_schema("plain", ["null", "int"])
        self.lake.append("p", rev, [u(1, 3)])
        self.lake.create_job("queued", ["p"], rev)
        self.lake.create_job("plain_job", ["p"], rev)
        self.lake.run_job("plain_job")
        with self.assertRaises(DataError):
            self.lake.export_projection("queued", {"root": "s", "states": {
                "s": {"record": "Root", "fields": {}}}})
        with self.assertRaises(DataError):
            self.lake.export_projection("plain_job", {"root": "s", "states": {
                "s": {"record": "Root", "fields": {}}}})
        with self.assertRaises(NotFound):
            self.lake.export_projection("absent", {"root": "s", "states": {
                "s": {"record": "Root", "fields": {}}}})
        with self.assertRaises(DataError):
            self.lake.export_projection("", {"root": "s", "states": {
                "s": {"record": "Root", "fields": {}}}})
        with self.assertRaises(DataError):
            self.lake.read_projection("")
        with self.assertRaises(NotFound):
            self.lake.read_projection("absent")

    def test_failed_job_cannot_be_exported(self):
        writer = {"type": "record", "name": "E", "fields": [
            {"name": "code", "type": {"type": "enum", "name": "C", "symbols": ["a"]}}]}
        reader = {"type": "record", "name": "E", "fields": [
            {"name": "code", "type": {"type": "enum", "name": "C", "symbols": ["b"]}}]}
        w = self.lake.register_schema("ew", writer)
        r = self.lake.register_schema("er", reader)
        self.lake.append("ef", w, [{"code": "a"}])
        self.lake.create_job("failed", ["ef"], r)
        with self.assertRaises(ResolutionError):
            self.lake.run_job("failed")
        with self.assertRaises(DataError):
            self.lake.export_projection("failed", {"root": "s", "states": {
                "s": {"record": "E", "fields": {}}}})

    def test_reordered_states_and_fields_are_same_identity(self):
        selection = {"root": "s", "states": {
            "s": {"record": "Root", "fields": {"n": {}, "a": {"A": "t"}}},
            "t": {"record": "A", "fields": {"v": {}, "w": {}}}}}
        first = self.lake.export_projection("j", selection)
        reordered = {"states": {
            "t": {"fields": {"w": {}, "v": {}}, "record": "A"},
            "s": {"fields": {"a": {"A": "t"}, "n": {}}, "record": "Root"}},
            "root": "s"}
        self.assertEqual(self.lake.export_projection("j", reordered), first)


class TestAtomicityAndReopen(unittest.TestCase):
    def test_memory_rollback_then_retry(self):
        with Lake() as lake:
            seed(lake)
            snapshot = schema_state(lake)
            fault = FailOnce("publish.before_commit")
            lake.db.failpoint = fault
            with self.assertRaises(OSError):
                lake.export_projection("history", copy.deepcopy(SELECTION))
            self.assertTrue(fault.fired)
            self.assertEqual(schema_state(lake), snapshot)
            self.assertEqual(lake.db.conn.execute(
                "SELECT count(*) FROM projection_rows").fetchone()[0], 0)
            lake.db.failpoint = None
            meta = lake.export_projection("history", copy.deepcopy(SELECTION))
            self.assertEqual(lake.read_projection(meta["export_id"])["rows"], EXPECTED_ROWS)
            self.assertEqual(lake.export_projection("history", copy.deepcopy(SELECTION)), meta)

    def test_disk_reopen_is_self_contained(self):
        with TemporaryDirectory(prefix="lake-projection-") as directory:
            path = Path(directory) / "archive.sqlite"
            with Lake(path) as lake:
                seed(lake)
                meta = lake.export_projection("history", copy.deepcopy(SELECTION))
                exported = lake.read_projection(meta["export_id"])
                tagged = lake.read_file(meta["file_id"])
                source = lake.read_result("history")
            with Lake(path) as lake:
                self.assertEqual(lake.read_projection(meta["export_id"]), exported)
                self.assertEqual(lake.read_file(meta["file_id"]), tagged)
                self.assertEqual(lake.read_result("history"), source)
                self.assertEqual(lake.export_projection("history",
                                                        copy.deepcopy(SELECTION)), meta)
                tree = sdk1.Schema(exported["schema"])
                payload = sdk1.encode(exported["schema"],
                                      tagged["records"][0])
                self.assertEqual(sdk1.decode(exported["schema"], payload),
                                 tagged["records"][0])
                self.assertEqual(sdk1.resolve(exported["schema"], exported["schema"],
                                              tagged["records"][0]),
                                 exported["rows"][0])

    def test_source_receipt_and_states_unchanged(self):
        with Lake() as lake:
            source = seed(lake)
            meta = lake.export_projection("history", copy.deepcopy(SELECTION))
            self.assertEqual(lake.read_result("history"), source)
            self.assertEqual(lake.run_job("history"), source)
            self.assertEqual(lake.jobs.get("history")["receipt"], source["receipt"])
            # A separate export with a different selection creates a new identity.
            other = lake.export_projection("history", {"root": "even", "states": {
                "even": {"record": "Link", "fields": {"value": {}}},
                "odd": {"record": "Link", "fields": {"value": {}}}}})
            self.assertNotEqual(other["export_id"], meta["export_id"])
            self.assertNotEqual(other["file_id"], meta["file_id"])
            self.assertNotEqual(other["writer_revision"], meta["writer_revision"])

    def test_caller_file_at_projection_identity_is_not_overwritten(self):
        from lake.db import canonical, digest
        with Lake() as lake:
            seed(lake)
            export_id = "pexport-" + digest(
                ["projection-export", "history", canonical(SELECTION)])
            occupied = "pfile-" + digest(["projection-file", export_id])
            reader = lake.catalog.head("view")
            lake.append(occupied, reader, [{
                "value": 42, "choice": u(0, {"v": 1}), "next": u(1, None),
                "score": u(0, 7)}])
            before = lake.read_file(occupied)
            meta = lake.export_projection("history", copy.deepcopy(SELECTION))
            self.assertNotEqual(meta["file_id"], occupied)
            self.assertEqual(lake.read_file(occupied), before)
            self.assertEqual(len(lake.read_projection(meta["export_id"])["rows"]), 5)
            # No caller-visible name head was created or moved.
            heads = [row["name"] for row in lake.db.conn.execute(
                "SELECT name FROM schema_heads")]
            self.assertEqual(sorted(heads), ["source", "view"])


if __name__ == "__main__":
    unittest.main()
