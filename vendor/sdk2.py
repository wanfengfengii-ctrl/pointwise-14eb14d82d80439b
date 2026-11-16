"""SDK2 node-graph codec. No inline-schema converter or resolution API."""
import json
import struct
from lake.errors import SchemaError, DataError

MAGIC = b"NG2\x00"
KINDS = {"null", "int", "long", "string", "record", "enum", "union"}


def _names(value):
    return (isinstance(value, list)
            and all(isinstance(x, str) and x.isidentifier() for x in value)
            and len(set(value)) == len(value))


def validate(graph, root):
    if not isinstance(graph, dict) or not graph or not isinstance(root, str) or root not in graph:
        raise SchemaError("graph/root required")
    if any(not isinstance(key, str) or not key for key in graph):
        raise SchemaError("node IDs must be nonempty strings")
    definitions = set()
    for node in graph.values():
        if (not isinstance(node, dict) or not isinstance(node.get("kind"), str)
                or node["kind"] not in KINDS):
            raise SchemaError("invalid graph node")
        kind = node["kind"]
        edges = []
        if kind in ("record", "enum"):
            name = node.get("name")
            if not isinstance(name, str) or not name.isidentifier() or name in definitions:
                raise SchemaError("invalid or duplicate named node")
            definitions.add(name)
            if not _names(node.get("aliases", [])):
                raise SchemaError("invalid aliases")
        if kind == "union":
            if set(node) != {"kind", "branches"}:
                raise SchemaError("invalid union attributes")
            edges = node["branches"]
            if not isinstance(edges, list) or not edges:
                raise SchemaError("nonempty branches required")
        elif kind == "record":
            if set(node) - {"kind", "name", "aliases", "fields"}:
                raise SchemaError("invalid record attributes")
            fields = node.get("fields")
            if not isinstance(fields, list):
                raise SchemaError("fields required")
            used = set()
            for field in fields:
                if (not isinstance(field, dict)
                        or not isinstance(field.get("name"), str)
                        or not field["name"].isidentifier()
                        or not _names(field.get("aliases", []))
                        or set(field) - {"name", "target", "aliases", "default"}):
                    raise SchemaError("invalid graph field")
                keys = [field["name"]] + field.get("aliases", [])
                if len(set(keys)) != len(keys) or used.intersection(keys):
                    raise SchemaError("ambiguous graph field")
                used.update(keys)
                edges.append(field.get("target"))
        elif kind == "enum":
            if (set(node) - {"kind", "name", "aliases", "symbols", "default"}
                    or not _names(node.get("symbols")) or not node["symbols"]):
                raise SchemaError("invalid graph enum")
            if "default" in node and node["default"] not in node["symbols"]:
                raise SchemaError("invalid enum default")
        elif set(node) != {"kind"}:
            raise SchemaError("invalid primitive attributes")
        if any(not isinstance(edge, str) or edge not in graph for edge in edges):
            raise SchemaError("dangling graph edge")
    # Union-only cycles have no value-consuming boundary.
    def unions(key, active):
        if graph[key]["kind"] != "union":
            return
        if key in active:
            raise SchemaError("union-only cycle")
        for child in graph[key]["branches"]:
            unions(child, active | {key})
    for key in graph:
        unions(key, set())
    try:
        json.dumps(graph, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise SchemaError("graph metadata must be JSON") from exc


def encode(graph, root, value):
    validate(graph, root)
    chunks = [MAGIC]

    def emit(key, item):
        node = graph[key]
        kind = node["kind"]
        if kind == "null":
            if item is not None:
                raise DataError("expected null")
        elif kind in ("int", "long"):
            if type(item) is not int:
                raise DataError("expected integer")
            try:
                chunks.append(struct.pack(">i" if kind == "int" else ">q", item))
            except struct.error as exc:
                raise DataError("integer out of range") from exc
        elif kind == "string":
            if not isinstance(item, str):
                raise DataError("expected string")
            try:
                data = item.encode("utf-8")
                chunks.extend((struct.pack(">I", len(data)), data))
            except (UnicodeError, struct.error) as exc:
                raise DataError("invalid string") from exc
        elif kind == "enum":
            if not isinstance(item, str) or item not in node["symbols"]:
                raise DataError("invalid enum symbol")
            chunks.append(struct.pack(">I", node["symbols"].index(item)))
        elif kind == "union":
            if not isinstance(item, dict) or set(item) != {"branch", "value"}:
                raise DataError("union requires branch and value")
            branch = item["branch"]
            if type(branch) is not int or not 0 <= branch < len(node["branches"]):
                raise DataError("invalid union branch")
            chunks.append(struct.pack(">I", branch))
            emit(node["branches"][branch], item["value"])
        else:
            fields = node["fields"]
            if not isinstance(item, dict) or set(item) != {f["name"] for f in fields}:
                raise DataError("record requires exactly its fields")
            for field in fields:
                emit(field["target"], item[field["name"]])
    emit(root, value)
    return b"".join(chunks)


def decode(graph, root, payload):
    validate(graph, root)
    if not isinstance(payload, bytes) or not payload.startswith(MAGIC):
        raise DataError("invalid SDK2 header")
    position = len(MAGIC)

    def take(size):
        nonlocal position
        end = position + size
        if end > len(payload):
            raise DataError("truncated SDK2 payload")
        data = payload[position:end]
        position = end
        return data

    def ordinal():
        return struct.unpack(">I", take(4))[0]

    def read(key):
        node = graph[key]
        kind = node["kind"]
        if kind == "null":
            return None
        if kind in ("int", "long"):
            return struct.unpack(">i" if kind == "int" else ">q",
                                 take(4 if kind == "int" else 8))[0]
        if kind == "string":
            try:
                return take(ordinal()).decode("utf-8")
            except UnicodeError as exc:
                raise DataError("invalid UTF-8") from exc
        if kind == "enum":
            index = ordinal()
            if index >= len(node["symbols"]):
                raise DataError("invalid enum ordinal")
            return node["symbols"][index]
        if kind == "union":
            branch = ordinal()
            if branch >= len(node["branches"]):
                raise DataError("invalid union ordinal")
            return {"branch": branch, "value": read(node["branches"][branch])}
        return {field["name"]: read(field["target"]) for field in node["fields"]}
    value = read(root)
    if position != len(payload):
        raise DataError("trailing SDK2 bytes")
    return value
