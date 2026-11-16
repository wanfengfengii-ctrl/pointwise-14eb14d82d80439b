"""Finite legacy data and a reproducible, complete v1 SQLite archive builder."""
import argparse
import copy
import json
from . import Lake, ResolutionError


def union(branch, value):
    return {"branch": branch, "value": value}


NODE = {"type": "record", "name": "Node", "fields": [
    {"name": "value", "type": "int"},
    {"name": "next", "type": ["null", "Node"]}]}
REVERSED = {"type": "record", "name": "Node", "fields": [
    {"name": "value", "type": "int"},
    {"name": "next", "type": ["Node", "null"]},
    {"name": "score", "type": ["null", "int"]}]}
LINK = {"type": "record", "name": "Link", "aliases": ["Node"], "fields": [
    {"name": "value", "type": "long"},
    {"name": "next", "type": ["Link", "null"]},
    {"name": "score", "type": ["long", "null"], "default": 7},
    {"name": "label", "type": "string", "default": "imported"}]}
CHAIN = {"value": 1, "next": union(1, {"value": 2, "next": union(0, None)})}
OTHER_CHAIN = {"value": 3, "next": union(0, {
    "value": 4, "next": union(1, None), "score": union(1, 5)}), "score": union(0, None)}
EXPECTED_CHAIN = {"value": 1, "next": {
    "value": 2, "next": None, "score": 7, "label": "imported"},
    "score": 7, "label": "imported"}
EXPECTED_OTHER = {"value": 3, "next": {
    "value": 4, "next": None, "score": 5, "label": "imported"},
    "score": None, "label": "imported"}

ENUM_WRITER = {"type": "record", "name": "Event", "fields": [
    {"name": "code", "type": {"type": "enum", "name": "Code", "symbols": ["ok", "late"]}}]}
ENUM_STRICT = {"type": "record", "name": "Event", "fields": [
    {"name": "code", "type": {"type": "enum", "name": "Code", "symbols": ["ok"]}}]}
ENUM_FALLBACK = copy.deepcopy(ENUM_STRICT)
ENUM_FALLBACK["fields"][0]["type"]["default"] = "ok"


def seed(lake):
    """Use an empty Lake; returns identities, not a precomputed database image."""
    old = lake.register_schema("source", NODE)
    reversed_rev = lake.register_schema("source", REVERSED)
    reader = lake.register_schema("view", LINK)
    old_receipt = lake.append("old", old, [CHAIN, CHAIN])
    lake.append("reversed", reversed_rev, [OTHER_CHAIN])
    lake.create_job("history", ["old", "reversed", "old"], reader)
    lake.run_job("history")
    lake.create_job("pending", ["reversed", "old"], reader)
    changed = copy.deepcopy(LINK)
    changed["fields"][-1]["default"] = "new"
    latest_reader = lake.register_schema("view", changed)
    ew = lake.register_schema("events", ENUM_WRITER)
    er = lake.register_schema("strict", ENUM_STRICT)
    ef = lake.register_schema("fallback", ENUM_FALLBACK)
    lake.append("events", ew, [{"code": "ok"}, {"code": "late"}])
    lake.create_job("rejected", ["events"], er)
    try:
        lake.run_job("rejected")
    except ResolutionError:
        pass
    lake.create_job("fallback", ["events"], ef)
    lake.run_job("fallback")
    return {"old": old, "reversed": reversed_rev, "reader": reader,
            "latest_reader": latest_reader, "old_receipt": old_receipt}


def main():
    parser = argparse.ArgumentParser(description="Build a finite v1 archive in an empty database")
    parser.add_argument("path")
    args = parser.parse_args()
    with Lake(args.path) as lake:
        if lake.db.conn.execute("SELECT count(*) FROM schema_revisions").fetchone()[0]:
            parser.error("sample builder requires an empty archive")
        print(json.dumps(seed(lake), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
