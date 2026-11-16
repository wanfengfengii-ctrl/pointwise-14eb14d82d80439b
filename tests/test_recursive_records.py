from lake.samples import NODE, REVERSED, LINK, CHAIN, OTHER_CHAIN, EXPECTED_CHAIN, EXPECTED_OTHER, union
from .support import MemoryCase


class TestRecursiveRecords(MemoryCase):
    def test_opposite_writer_union_orders_recursive_alias_and_duplicates(self):
        a = self.lake.register_schema("source", NODE)
        b = self.lake.register_schema("source", REVERSED)
        r = self.lake.register_schema("view", LINK)
        self.lake.append("a", a, [CHAIN, CHAIN])
        self.lake.append("b", b, [OTHER_CHAIN])
        self.lake.create_job("j", ["a", "b", "a"], r)
        result = self.lake.run_job("j")
        self.assertEqual(result["rows"], [EXPECTED_CHAIN, EXPECTED_CHAIN, EXPECTED_OTHER,
                                          EXPECTED_CHAIN, EXPECTED_CHAIN])
        self.assertEqual(result["row_ids"],
                         [["a", 0, 0], ["a", 0, 1], ["b", 1, 0], ["a", 2, 0], ["a", 2, 1]])
        self.assertEqual(self.lake.read_file("b")["records"], [OTHER_CHAIN])

    def test_mutually_recursive_named_types_and_reader_aliases(self):
        writer = {"type": "record", "name": "A", "fields": [{"name": "b", "type": [
            "null", {"type": "record", "name": "B", "fields": [
                {"name": "n", "type": "int"}, {"name": "a", "type": ["null", "A"]}]}]}]}
        reader = {"type": "record", "name": "X", "aliases": ["A"], "fields": [
            {"name": "b", "type": [
                {"type": "record", "name": "Y", "aliases": ["B"], "fields": [
                    {"name": "n", "type": "long"}, {"name": "a", "type": ["X", "null"]}]},
                "null"]}]}
        value = {"b": union(1, {"n": 9, "a": union(1, {"b": union(0, None)})})}
        w = self.lake.register_schema("w", writer)
        r = self.lake.register_schema("r", reader)
        self.lake.append("f", w, [value])
        self.lake.create_job("j", ["f"], r)
        self.assertEqual(self.lake.run_job("j")["rows"],
                         [{"b": {"n": 9, "a": {"b": None}}}])
