import copy
import unittest
from lake import DataError, SchemaError, ResolutionError
from lake.samples import NODE, CHAIN, LINK, EXPECTED_CHAIN, union
from vendor import sdk1, sdk2


def node_graph():
    # Authored natively: no conversion from inline schemas.
    return {
        "n": {"kind": "null"},
        "i": {"kind": "int"},
        "choice": {"kind": "union", "branches": ["n", "record"]},
        "record": {"kind": "record", "name": "Node", "fields": [
            {"name": "value", "target": "i"}, {"name": "next", "target": "choice"}]},
    }


class TestPorts(unittest.TestCase):
    def test_sdk1_frozen_wire_format_and_round_trip(self):
        self.assertEqual(sdk1.encode(NODE, CHAIN), b'IL1\n[1,[1,[2,[0,null]]]]')
        self.assertEqual(sdk1.decode(NODE, b'IL1\n[1,[1,[2,[0,null]]]]'), CHAIN)
        self.assertEqual(sdk1.resolve(NODE, LINK, CHAIN), EXPECTED_CHAIN)
        enum = {"type": "enum", "name": "E", "symbols": ["b", "a"]}
        self.assertEqual(sdk1.encode(enum, "a"), b"IL1\n1")

    def test_sdk2_frozen_wire_format_and_round_trip(self):
        graph = node_graph()
        payload = b"NG2\x00\x00\x00\x00\x01\x00\x00\x00\x01\x00\x00\x00\x02\x00\x00\x00\x00"
        self.assertEqual(sdk2.encode(graph, "record", CHAIN), payload)
        self.assertEqual(sdk2.decode(graph, "record", payload), CHAIN)
        enum_graph = {"e": {"kind": "enum", "name": "E", "symbols": ["b", "a"]}}
        self.assertEqual(sdk2.encode(enum_graph, "e", "a"), b"NG2\x00\x00\x00\x00\x01")

    def test_port_primitives_and_unions(self):
        values = {"null": None, "int": -(2**31), "long": 2**63 - 1, "string": "湖\u0000尾"}
        for kind, value in values.items():
            with self.subTest(kind=kind):
                self.assertEqual(sdk1.decode(kind, sdk1.encode(kind, value)), value)
                graph = {"root": {"kind": kind}}
                self.assertEqual(sdk2.decode(graph, "root", sdk2.encode(graph, "root", value)), value)
        for schema, value in [("int", True), ("int", 2**31), ("long", -(2**63)-1),
                              ("string", None), ("null", 0), (["null", "int"], union(-1, 1))]:
            with self.subTest(value=value), self.assertRaises(DataError):
                sdk1.encode(schema, value)
        graph = node_graph()
        with self.assertRaises(DataError):
            sdk2.encode(graph, "record", {"value": True, "next": union(0, None)})
        with self.assertRaises(DataError):
            sdk2.encode(graph, "choice", union(5, None))

    def test_truncated_corrupt_and_trailing_payloads(self):
        graph = node_graph()
        payload = sdk2.encode(graph, "record", CHAIN)
        for bad in [b"", payload[:-1], payload + b"x", b"IL1\n[]"]:
            with self.subTest(bad=bad), self.assertRaises(DataError):
                sdk2.decode(graph, "record", bad)
        for bad in [b"", b"IL1\n[1]", b"IL1\n[1,[4,null]]", b"IL1\n[true,[0,null]]"]:
            with self.subTest(bad=bad), self.assertRaises(DataError):
                sdk1.decode(NODE, bad)

    def test_graph_validation(self):
        graph = node_graph()
        with self.assertRaises(SchemaError):
            sdk2.validate({"x": {"kind": []}}, "x")
        bad = copy.deepcopy(graph)
        bad["choice"]["branches"][1] = "absent"
        with self.assertRaises(SchemaError):
            sdk2.validate(bad, "record")
        with self.assertRaises(SchemaError):
            sdk2.validate({"a": {"kind": "union", "branches": ["a"]}}, "a")
        with self.assertRaises(SchemaError):
            sdk2.validate(graph, "absent")

    def test_unselected_branch_is_not_resolved(self):
        schema = ["null", {"type": "record", "name": "Unused", "fields": [{"name": "x", "type": "int"}]}]
        self.assertIsNone(sdk1.resolve(schema, ["null", "string"], union(0, None)))
        with self.assertRaises(ResolutionError):
            sdk1.resolve(schema, ["null", "string"], union(1, {"x": 1}))

    def test_first_matching_reader_branch_and_no_value_fallback(self):
        writer = {"type": "enum", "name": "E", "symbols": ["a", "b"]}
        reader = [{"type": "enum", "name": "R1", "aliases": ["E"], "symbols": ["a"]},
                  {"type": "enum", "name": "R2", "aliases": ["E"], "symbols": ["a", "b"]}]
        with self.assertRaises(ResolutionError):
            sdk1.resolve(writer, reader, "b")
        reader[0]["default"] = "a"
        self.assertEqual(sdk1.resolve(writer, reader, "b"), "a")
        self.assertEqual(sdk1.resolve("int", ["long", "int"], 8), 8)

    def test_type_aliases_belong_to_reader_and_default_does_not_override_null(self):
        writer = {"type": "record", "name": "New", "aliases": ["Old"], "fields": []}
        reader = {"type": "record", "name": "Old", "fields": []}
        with self.assertRaises(ResolutionError):
            sdk1.resolve(writer, reader, {})
        self.assertEqual(sdk1.resolve(reader, writer, {}), {})
        w = {"type": "record", "name": "R", "fields": [{"name": "x", "type": ["null", "int"]}]}
        r = {"type": "record", "name": "R", "fields": [{"name": "x", "type": "long", "default": 7}]}
        with self.assertRaises(ResolutionError):
            sdk1.resolve(w, r, {"x": union(0, None)})

    def test_recursive_record_default(self):
        writer = {"type": "record", "name": "R", "fields": []}
        reader = {"type": "record", "name": "R", "fields": [{
            "name": "child", "type": NODE, "default": {"value": 5, "next": None}}]}
        self.assertEqual(sdk1.resolve(writer, reader, {}),
                         {"child": {"value": 5, "next": None}})

    def test_native_graph_reordered_union_and_named_metadata(self):
        graph = node_graph()
        graph["choice"]["branches"] = ["record", "n"]
        graph["record"]["name"] = "Link"
        graph["record"]["aliases"] = ["Node"]
        graph["record"]["fields"].append({"name": "score", "target": "i", "default": 7})
        value = {"value": 1, "score": 7, "next": union(0, {
            "value": 2, "score": 9, "next": union(1, None)})}
        self.assertEqual(sdk2.decode(graph, "record", sdk2.encode(graph, "record", value)), value)
        missing = {"value": 1, "next": union(1, None)}
        with self.assertRaises(DataError):
            sdk2.encode(graph, "record", missing)
