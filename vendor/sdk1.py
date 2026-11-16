"""SDK1 inline schemas, tagged union values, and writer/reader resolution."""
import copy
import json
from lake.errors import SchemaError, DataError, ResolutionError

PRIMITIVES = {"null", "int", "long", "string"}
MAGIC = b"IL1\n"


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def identifier(value):
    return isinstance(value, str) and bool(value) and value.isidentifier()


def aliases(obj):
    values = obj.get("aliases", [])
    if (not isinstance(values, list) or any(not identifier(x) for x in values)
            or len(set(values)) != len(values)):
        raise SchemaError("aliases must be unique identifiers")
    return values


class Schema:
    def __init__(self, schema):
        self.root = copy.deepcopy(schema)
        self.names = {}
        self.fields = []
        self._collect(self.root)
        self._references(self.root)
        for field in self.fields:
            if "default" in field:
                try:
                    value = self.default(field["type"], field["default"])
                    self.transcode(field["type"], value)
                except (DataError, RecursionError) as exc:
                    raise SchemaError("invalid field default: " + field["name"]) from exc

    def _collect(self, node):
        if isinstance(node, str):
            if node not in PRIMITIVES and not identifier(node):
                raise SchemaError("invalid type reference")
            return
        if isinstance(node, list):
            if not node:
                raise SchemaError("empty union")
            for branch in node:
                self._collect(branch)
            return
        if not isinstance(node, dict) or node.get("type") not in ("record", "enum"):
            raise SchemaError("expected primitive, reference, union, record or enum")
        kind = node["type"]
        name = node.get("name")
        if not identifier(name) or name in PRIMITIVES or name in self.names:
            raise SchemaError("invalid or duplicate named definition")
        self.names[name] = node
        aliases(node)
        if kind == "enum":
            if set(node) - {"type", "name", "aliases", "symbols", "default"}:
                raise SchemaError("unknown enum attribute")
            symbols = node.get("symbols")
            if (not isinstance(symbols, list) or not symbols
                    or any(not identifier(x) for x in symbols)
                    or len(set(symbols)) != len(symbols)):
                raise SchemaError("invalid enum symbols")
            if "default" in node and node["default"] not in symbols:
                raise SchemaError("enum default is not a symbol")
            return
        if set(node) - {"type", "name", "aliases", "fields"}:
            raise SchemaError("unknown record attribute")
        fields = node.get("fields")
        if not isinstance(fields, list):
            raise SchemaError("fields must be a list")
        used = set()
        for field in fields:
            if (not isinstance(field, dict) or "type" not in field
                    or not identifier(field.get("name"))
                    or set(field) - {"name", "type", "aliases", "default"}):
                raise SchemaError("invalid record field")
            keys = [field["name"]] + aliases(field)
            if len(set(keys)) != len(keys) or used.intersection(keys):
                raise SchemaError("ambiguous field name or alias")
            used.update(keys)
            self.fields.append(field)
            self._collect(field["type"])

    def _references(self, node):
        if isinstance(node, str):
            if node not in PRIMITIVES and node not in self.names:
                raise SchemaError("undefined type: " + node)
        elif isinstance(node, list):
            for branch in node:
                self._references(branch)
        elif node["type"] == "record":
            for field in node["fields"]:
                self._references(field["type"])

    def deref(self, node):
        return self.names[node] if isinstance(node, str) and node not in PRIMITIVES else node

    def default(self, node, value):
        node = self.deref(node)
        if isinstance(node, list):
            return {"branch": 0, "value": self.default(node[0], value)}
        if isinstance(node, dict) and node["type"] == "record":
            if not isinstance(value, dict) or set(value) != {f["name"] for f in node["fields"]}:
                raise DataError("default record needs every field")
            return {f["name"]: self.default(f["type"], value[f["name"]])
                    for f in node["fields"]}
        return copy.deepcopy(value)

    def transcode(self, node, value, reading=False):
        node = self.deref(node)
        if isinstance(node, list):
            if reading:
                if not isinstance(value, list) or len(value) != 2:
                    raise DataError("invalid wire union")
                branch, item = value
            else:
                if not isinstance(value, dict) or set(value) != {"branch", "value"}:
                    raise DataError("union requires branch and value")
                branch, item = value["branch"], value["value"]
            if type(branch) is not int or not 0 <= branch < len(node):
                raise DataError("union branch out of range")
            item = self.transcode(node[branch], item, reading)
            return {"branch": branch, "value": item} if reading else [branch, item]
        if isinstance(node, str):
            if node == "null":
                valid = value is None
            elif node in ("int", "long"):
                bits = 32 if node == "int" else 64
                valid = type(value) is int and -(2 ** (bits - 1)) <= value < 2 ** (bits - 1)
            else:
                valid = isinstance(value, str)
                if valid:
                    try:
                        value.encode("utf-8")
                    except UnicodeError:
                        valid = False
            if not valid:
                raise DataError("invalid " + node + " value")
            return value
        if node["type"] == "enum":
            symbols = node["symbols"]
            if reading:
                if type(value) is not int or not 0 <= value < len(symbols):
                    raise DataError("invalid enum ordinal")
                return symbols[value]
            if not isinstance(value, str) or value not in symbols:
                raise DataError("invalid enum symbol")
            return symbols.index(value)
        fields = node["fields"]
        if reading:
            if not isinstance(value, list) or len(value) != len(fields):
                raise DataError("invalid wire record")
            return {f["name"]: self.transcode(f["type"], v, True)
                    for f, v in zip(fields, value)}
        if not isinstance(value, dict) or set(value) != {f["name"] for f in fields}:
            raise DataError("record requires exactly its writer fields")
        return [self.transcode(f["type"], value[f["name"]]) for f in fields]


def encode(schema, value):
    tree = Schema(schema)
    try:
        return MAGIC + dumps(tree.transcode(tree.root, value)).encode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise DataError("invalid value encoding") from exc


def decode(schema, payload):
    tree = Schema(schema)
    if not isinstance(payload, bytes) or not payload.startswith(MAGIC):
        raise DataError("invalid SDK1 header")
    try:
        wire = json.loads(payload[len(MAGIC):].decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise DataError("invalid SDK1 JSON") from exc
    return tree.transcode(tree.root, wire, True)


def _matches(writer, reader):
    if isinstance(writer, str) or isinstance(reader, str):
        return writer == reader or (writer == "int" and reader == "long")
    if writer["type"] != reader["type"]:
        return False
    return writer["name"] == reader["name"] or writer["name"] in reader.get("aliases", [])


def _reader_branch(w, tree, r):
    r = tree.deref(r)
    if isinstance(r, list):
        for branch in r:
            found = _reader_branch(w, tree, branch)
            if found is not None:
                return found
        return None
    return r if _matches(w, r) else None


def _resolve(wtree, w, rtree, r, value):
    w, r = wtree.deref(w), rtree.deref(r)
    if isinstance(w, list):
        branch = value["branch"]
        return _resolve(wtree, w[branch], rtree, r, value["value"])
    if isinstance(r, list):
        candidate = _reader_branch(w, rtree, r)
        if candidate is None:
            raise ResolutionError("no compatible reader union branch")
        return _resolve(wtree, w, rtree, candidate, value)
    if not _matches(w, r):
        raise ResolutionError("incompatible writer and reader types")
    if isinstance(w, str):
        return value
    if w["type"] == "enum":
        if value in r["symbols"]:
            return value
        if "default" in r:
            return r["default"]
        raise ResolutionError("reader enum lacks symbol: " + value)
    writer_fields = {f["name"]: f for f in w["fields"]}
    result = {}
    for field in r["fields"]:
        source = next((writer_fields[n] for n in [field["name"]] + field.get("aliases", [])
                       if n in writer_fields), None)
        if source is None:
            if "default" not in field:
                raise ResolutionError("missing reader field: " + field["name"])
            default = rtree.default(field["type"], field["default"])
            result[field["name"]] = _resolve(rtree, field["type"], rtree, field["type"], default)
        else:
            result[field["name"]] = _resolve(wtree, source["type"], rtree,
                                             field["type"], value[source["name"]])
    return result


def resolve(writer_schema, reader_schema, value):
    writer, reader = Schema(writer_schema), Schema(reader_schema)
    # Validate the entire writer record, including fields discarded by the reader.
    checked = writer.transcode(writer.root,
                               writer.transcode(writer.root, value), True)
    return _resolve(writer, writer.root, reader, reader.root, checked)
