"""Public source fixtures and hand-written expectations, not a projection implementation."""
import copy
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from lake import Lake, FailOnce, SchemaError
from vendor import sdk1


def u(branch, value):
    return {"branch": branch, "value": value}


A = {"type": "record", "name": "A",
     "fields": [{"name": "v", "type": "int"}]}
B = {"type": "record", "name": "B",
     "fields": [{"name": "v", "type": "int"}]}
OLD = {"type": "record", "name": "Node", "fields": [
    {"name": "value", "type": "int"},
    {"name": "choice", "type": [A, B]},
    {"name": "next", "type": ["null", "Node"]}]}
NEW = {"type": "record", "name": "Node", "fields": [
    {"name": "value", "type": "int"},
    {"name": "choice", "type": [B, A]},
    {"name": "next", "type": ["Node", "null"]},
    {"name": "score", "type": ["null", "int"]}]}
READER = {"type": "record", "name": "Link", "aliases": ["Node"], "fields": [
    {"name": "value", "type": "long"},
    {"name": "choice", "type": [B, A]},
    {"name": "next", "type": ["Link", "null"]},
    {"name": "score", "type": ["long", "null"], "default": 7}]}
CHAIN = {"value": 1, "choice": u(0, {"v": 7}), "next": u(1, {
    "value": 2, "choice": u(1, {"v": 7}), "next": u(1, {
        "value": 3, "choice": u(0, {"v": 9}), "next": u(0, None)})})}
OTHER = {"value": 4, "choice": u(0, {"v": 7}),
         "next": u(1, None), "score": u(0, None)}
SOURCE_CHAIN = {"value": 1, "choice": {"v": 7}, "next": {
    "value": 2, "choice": {"v": 7}, "next": {
        "value": 3, "choice": {"v": 9}, "next": None, "score": 7},
    "score": 7}, "score": 7}
SOURCE_OTHER = {"value": 4, "choice": {"v": 7}, "next": None, "score": None}

SELECTION = {"root": "even", "states": {
    "even": {"record": "Link", "fields": {
        "value": {}, "choice": {"A": "a", "B": "b"},
        "next": {"Link": "odd"}, "score": {}}},
    "odd": {"record": "Link", "fields": {
        "choice": {"A": "a", "B": "b"},
        "next": {"Link": "even"}, "score": {}}},
    "a": {"record": "A", "fields": {}},
    "b": {"record": "B", "fields": {}}}}
PROJECTED_CHAIN = {"value": 1, "choice": {}, "next": {
    "choice": {}, "next": {
        "value": 3, "choice": {}, "next": None, "score": 7},
    "score": 7}, "score": 7}
PROJECTED_OTHER = {"value": 4, "choice": {}, "next": None, "score": None}
EXPECTED_ROWS = [PROJECTED_CHAIN, PROJECTED_CHAIN, PROJECTED_OTHER,
                 PROJECTED_CHAIN, PROJECTED_CHAIN]
EXPECTED_IDS = [["old", 0, 0], ["old", 0, 1], ["new", 1, 0],
                ["old", 2, 0], ["old", 2, 1]]


def seed(lake):
    old = lake.register_schema("source", OLD)
    new = lake.register_schema("source", NEW)
    reader = lake.register_schema("view", READER)
    lake.append("old", old, [CHAIN, CHAIN])
    lake.append("new", new, [OTHER])
    lake.create_job("history", ["old", "new", "old"], reader)
    result = lake.run_job("history")
    assert result["rows"] == [SOURCE_CHAIN, SOURCE_CHAIN, SOURCE_OTHER,
                              SOURCE_CHAIN, SOURCE_CHAIN]
    assert result["row_ids"] == EXPECTED_IDS
    return result


def fixtures():
    for schema in (OLD, NEW, READER):
        sdk1.Schema(schema)
    assert sdk1.resolve(OLD, READER, CHAIN) == SOURCE_CHAIN
    assert sdk1.resolve(NEW, READER, OTHER) == SOURCE_OTHER
    with Lake() as lake:
        result = seed(lake)
        print(json.dumps({"source": result, "selection": SELECTION,
                          "expected_projection_rows": EXPECTED_ROWS},
                         ensure_ascii=False, sort_keys=True))


def base_state(lake):
    conn = lake.db.conn
    return {
        "revisions": [tuple(r) for r in conn.execute(
            "SELECT * FROM schema_revisions ORDER BY revision")],
        "heads": [tuple(r) for r in conn.execute(
            "SELECT * FROM schema_heads ORDER BY name")],
        "files": [tuple(r) for r in conn.execute(
            "SELECT * FROM files ORDER BY file_id")],
        "source": lake.read_result("history")}


def verify():
    with TemporaryDirectory(prefix="projection-example-") as directory:
        path = Path(directory) / "lake.sqlite"
        with Lake(path) as lake:
            source = seed(lake)
            # Change the head after success; export must still use history's reader.
            changed = copy.deepcopy(READER)
            changed["fields"][-1]["default"] = 99
            lake.register_schema("view", changed)
            before = base_state(lake)
            bad = copy.deepcopy(SELECTION)
            bad["states"]["even"]["fields"]["next"]["Link"] = "a"
            try:
                lake.export_projection("history", bad)
            except SchemaError:
                pass
            else:
                raise AssertionError("wrong-record transition was accepted")
            assert base_state(lake) == before
            fault = FailOnce("publish.before_commit")
            lake.db.failpoint = fault
            try:
                lake.export_projection("history", copy.deepcopy(SELECTION))
            except OSError:
                pass
            else:
                raise AssertionError("export did not reach the failure boundary")
            assert fault.fired
            assert base_state(lake) == before
            lake.db.failpoint = None
            request = copy.deepcopy(SELECTION)
            meta = lake.export_projection("history", request)
            frozen_meta = copy.deepcopy(meta)
            assert set(meta) == {"export_id", "job_id", "file_id",
                                 "writer_revision", "row_count", "receipt"}
            assert meta["job_id"] == "history" and meta["row_count"] == 5
            exported = lake.read_projection(meta["export_id"])
            assert exported == dict(meta, schema=lake.catalog.get(meta["writer_revision"]),
                                    rows=EXPECTED_ROWS, row_ids=EXPECTED_IDS)
            archive = lake.read_file(meta["file_id"])
            assert archive["writer_revision"] == meta["writer_revision"]
            records = archive["records"]
            assert len(records) == 5
            first = records[0]
            assert first["choice"] == u(1, {})
            assert first["next"]["branch"] == 0
            middle = first["next"]["value"]
            assert "value" not in middle and middle["choice"] == u(0, {})
            assert middle["next"]["branch"] == 0
            last = middle["next"]["value"]
            assert last["value"] == 3 and last["choice"] == u(1, {})
            assert last["next"] == u(1, None)
            assert first["score"] == middle["score"] == last["score"] == u(0, 7)
            assert records[2]["choice"] == u(0, {})
            assert records[2]["score"] == u(1, None)
            tree = sdk1.Schema(exported["schema"])
            root = tree.deref(tree.root)
            assert [f["name"] for f in root["fields"]] == ["value", "choice", "next", "score"]
            child_type = next(f["type"] for f in root["fields"] if f["name"] == "next")
            child = tree.deref(child_type[0])
            assert [f["name"] for f in child["fields"]] == ["choice", "next", "score"]
            for record, expected in zip(records, EXPECTED_ROWS):
                payload = sdk1.encode(exported["schema"], record)
                assert sdk1.decode(exported["schema"], payload) == record
                assert sdk1.resolve(exported["schema"], exported["schema"], record) == expected
            after = base_state(lake)
            reordered = json.loads(json.dumps(SELECTION, sort_keys=True))
            assert lake.export_projection("history", reordered) == frozen_meta
            assert base_state(lake) == after
            request["states"].clear()
            meta["row_count"] = -1
            altered = lake.read_projection(frozen_meta["export_id"])
            altered["rows"].clear()
            assert lake.read_projection(frozen_meta["export_id"]) == exported
            assert lake.read_result("history") == source
        with Lake(path) as lake:
            assert lake.read_projection(frozen_meta["export_id"]) == exported
            assert lake.read_file(frozen_meta["file_id"]) == archive
            assert lake.export_projection("history", copy.deepcopy(SELECTION)) == frozen_meta
            print(json.dumps({"reopened_projection": exported,
                              "tagged_records": lake.read_file(frozen_meta["file_id"])["records"]},
                             ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) == 2 else "fixtures"
    if mode == "fixtures":
        fixtures()
    elif mode == "verify":
        verify()
    else:
        raise SystemExit("usage: projection_examples.py [fixtures|verify]")
