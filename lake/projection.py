"""Finite-state projection export for fixed succeeded job results.

The source of truth for a projection is the succeeded job's fixed
``reader_revision`` plus the fixed result rows.  Reader union tags are
recomputed with the original SDK1 resolution rules, never guessed from the
untagged result rows, and then carried into a freshly synthesized, closed
SDK1 schema.
"""
from vendor import sdk1
from .db import canonical, digest, identity
from .errors import Conflict, DataError, NotFound, ResolutionError, SchemaError


# ---------------------------------------------------------------------------
# Tagged writer -> reader resolution
#
# This mirrors vendor.sdk1._resolve exactly, except that every reader union
# level is kept in the produced value.  The logical value is obtained by
# removing those wrappers with strip_tags().
# ---------------------------------------------------------------------------

def _matches(writer, reader):
    if isinstance(writer, str) or isinstance(reader, str):
        return writer == reader or (writer == "int" and reader == "long")
    if writer["type"] != reader["type"]:
        return False
    return writer["name"] == reader["name"] or writer["name"] in reader.get("aliases", [])


def _reader_path(writer, tree, reader):
    """Return ([branch, ...], matching_leaf) mirroring sdk1's depth-first match."""
    reader = tree.deref(reader)
    if isinstance(reader, list):
        for index, branch in enumerate(reader):
            found = _reader_path(writer, tree, branch)
            if found is not None:
                path, leaf = found
                return [index] + path, leaf
        return None
    return ([], reader) if _matches(writer, reader) else None


def _resolve_tagged(wtree, w, rtree, r, value):
    w, r = wtree.deref(w), rtree.deref(r)
    if isinstance(w, list):
        if not isinstance(value, dict) or set(value) != {"branch", "value"}:
            raise DataError("writer union requires branch and value")
        branch = value["branch"]
        if type(branch) is not int or not 0 <= branch < len(w):
            raise DataError("union branch out of range")
        return _resolve_tagged(wtree, w[branch], rtree, r, value["value"])
    if isinstance(r, list):
        found = _reader_path(w, rtree, r)
        if found is None:
            raise ResolutionError("no compatible reader union branch")
        path, candidate = found
        item = _resolve_tagged(wtree, w, rtree, candidate, value)
        for index in reversed(path):
            item = {"branch": index, "value": item}
        return item
    if not _matches(w, r):
        raise ResolutionError("incompatible writer and reader types")
    if isinstance(w, str):
        return value
    if w["type"] == "enum":
        if value in r["symbols"]:
            return value
        if "default" in r:
            return r["default"]
        raise ResolutionError("reader enum lacks symbol: " + str(value))
    writer_fields = {field["name"]: field for field in w["fields"]}
    result = {}
    for field in r["fields"]:
        source = next((writer_fields[name] for name in [field["name"]] + field.get("aliases", [])
                       if name in writer_fields), None)
        if source is None:
            if "default" not in field:
                raise ResolutionError("missing reader field: " + field["name"])
            default = rtree.default(field["type"], field["default"])
            result[field["name"]] = _resolve_tagged(
                rtree, field["type"], rtree, field["type"], default)
        else:
            result[field["name"]] = _resolve_tagged(
                wtree, source["type"], rtree, field["type"], value[source["name"]])
    return result


def tagged_resolve(writer_doc, reader_doc, value, writer_tree=None, reader_tree=None):
    """Resolve one writer record to a reader-shaped value that keeps reader union tags.

    The writer record is fully validated (including fields the reader drops),
    exactly like sdk1.resolve.  Reader field defaults and explicit nulls follow
    the original SDK semantics.
    """
    wtree = writer_tree if writer_tree is not None else sdk1.Schema(writer_doc)
    rtree = reader_tree if reader_tree is not None else sdk1.Schema(reader_doc)
    checked = wtree.transcode(wtree.root, wtree.transcode(wtree.root, value), True)
    return _resolve_tagged(wtree, wtree.root, rtree, rtree.root, checked)


def strip_tags(tree, node, value):
    """Remove union wrappers using the schema that produced ``value``."""
    node = tree.deref(node)
    if isinstance(node, list):
        if not isinstance(value, dict) or set(value) != {"branch", "value"}:
            raise DataError("tagged union has invalid shape")
        return strip_tags(tree, node[value["branch"]], value["value"])
    if isinstance(node, str):
        return value
    if node["type"] == "enum":
        return value
    return {field["name"]: strip_tags(tree, field["type"], value[field["name"]])
            for field in node["fields"]}


# ---------------------------------------------------------------------------
# Selection validation
# ---------------------------------------------------------------------------

def _record_leaves(tree, node):
    """Formal record names reachable without entering another record."""
    node = tree.deref(node)
    if isinstance(node, str):
        return []
    if isinstance(node, list):
        leaves = []
        for branch in node:
            for name in _record_leaves(tree, branch):
                if name not in leaves:
                    leaves.append(name)
        return leaves
    if node["type"] == "record":
        return [node["name"]]
    return []


def compile_selection(selection, tree, source_root):
    """Validate a selection against the fixed reader schema and normalize it.

    ``source_root`` is the formal record name bound by the source root.
    Raises SchemaError for every malformed or mismatched selection.
    """
    if not isinstance(selection, dict) or set(selection) != {"root", "states"}:
        raise SchemaError("selection must contain exactly root and states")
    root = selection["root"]
    states = selection["states"]
    if not isinstance(root, str) or not root or not root.isidentifier():
        raise SchemaError("invalid root state name")
    if not isinstance(states, dict) or not states:
        raise SchemaError("states must be a nonempty object")

    compiled = {}
    for name, spec in states.items():
        if not isinstance(name, str) or not name.isidentifier():
            raise SchemaError("invalid state name")
        if not isinstance(spec, dict) or set(spec) != {"record", "fields"}:
            raise SchemaError("state must contain exactly record and fields")
        record = spec["record"]
        if not isinstance(record, str) or not record.isidentifier():
            raise SchemaError("invalid state record name")
        node = tree.names.get(record)
        if node is None or node.get("type") != "record":
            raise SchemaError("unknown source record: " + str(record))
        if not isinstance(spec["fields"], dict):
            raise SchemaError("fields must be an object")
        compiled[name] = {"record": record, "node": node, "fields": spec["fields"]}

    if root not in compiled:
        raise SchemaError("unknown root state: " + root)
    if compiled[root]["record"] != source_root:
        raise SchemaError("root state does not bind the source root record")

    for name, state in compiled.items():
        declared = {field["name"]: field for field in state["node"]["fields"]}
        transitions = {}
        for field_name, mapping in state["fields"].items():
            if not isinstance(field_name, str):
                raise SchemaError("invalid field name")
            field = declared.get(field_name)
            if field is None:
                raise SchemaError("unknown field: " + str(field_name))
            if not isinstance(mapping, dict):
                raise SchemaError("field transition must be an object")
            leaves = _record_leaves(tree, field["type"])
            if set(mapping) != set(leaves):
                raise SchemaError("transitions must cover exactly the record leaves of "
                                  + str(field_name))
            for leaf, target in mapping.items():
                if not isinstance(leaf, str) or leaf not in leaves:
                    raise SchemaError("unknown record leaf: " + str(leaf))
                if not isinstance(target, str) or not target.isidentifier():
                    raise SchemaError("invalid target state name")
                if target not in compiled:
                    raise SchemaError("unknown target state: " + target)
                if compiled[target]["record"] != leaf:
                    raise SchemaError("state binds the wrong record for leaf: " + leaf)
            transitions[field_name] = dict(mapping)
        state["transitions"] = transitions

    return {"root": root, "states": compiled, "source_root": source_root}


# ---------------------------------------------------------------------------
# Synthesized output schema
# ---------------------------------------------------------------------------

class SchemaBuilder:
    """Build a closed SDK1 schema for one selection.

    One named record definition is created per (source record, state) pair
    actually reachable from the output root.  Shapes from different states are
    never merged.  Aliases and defaults are not copied; enum symbols stay in
    their declared order.
    """

    def __init__(self, tree, plan):
        self.tree = tree
        self.plan = plan
        self.used = set(tree.names)
        self.records = {}
        self.enums = set()

    def _allocate(self, record, state):
        base = record + "__" + state
        name, suffix = base, 1
        while name in self.used:
            name = base + "_" + str(suffix)
            suffix += 1
        self.used.add(name)
        return name

    def build(self):
        return self.record(self.plan["source_root"], self.plan["root"])

    def record(self, record, state):
        key = (record, state)
        if key in self.records:
            return self.records[key]
        output_name = self._allocate(record, state)
        self.records[key] = output_name
        source = self.tree.names[record]
        transitions = self.plan["states"][state]["transitions"]
        fields = []
        for field in source["fields"]:
            if field["name"] in transitions:
                fields.append({"name": field["name"],
                               "type": self.type(field["type"], transitions[field["name"]])})
        return {"type": "record", "name": output_name, "fields": fields}

    def type(self, node, transitions):
        node = self.tree.deref(node)
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            return [self.type(branch, transitions) for branch in node]
        if node["type"] == "enum":
            return self.enum(node)
        record = node["name"]
        if record not in transitions:
            raise SchemaError("missing transition for record leaf: " + record)
        return self.record(record, transitions[record])

    def enum(self, node):
        name = node["name"]
        if name in self.enums:
            return name
        self.enums.add(name)
        return {"type": "enum", "name": name, "symbols": list(node["symbols"])}


class Projector:
    """Transform tagged reader values into tagged values of the output schema."""

    def __init__(self, tree, plan):
        self.tree = tree
        self.plan = plan

    def row(self, value):
        return self.record(self.plan["source_root"], self.plan["root"], value)

    def record(self, record, state, value):
        transitions = self.plan["states"][state]["transitions"]
        source = self.tree.names[record]
        result = {}
        for field in source["fields"]:
            if field["name"] in transitions:
                result[field["name"]] = self.type(
                    field["type"], transitions[field["name"]], value[field["name"]])
        return result

    def type(self, node, transitions, value):
        node = self.tree.deref(node)
        if isinstance(node, str):
            return value
        if isinstance(node, list):
            if not isinstance(value, dict) or set(value) != {"branch", "value"}:
                raise DataError("tagged union has invalid shape")
            branch = value["branch"]
            return {"branch": branch,
                    "value": self.type(node[branch], transitions, value["value"])}
        if node["type"] == "enum":
            return value
        return self.record(node["name"], transitions[node["name"]], value)


def _encode(tree, value):
    try:
        return sdk1.MAGIC + sdk1.dumps(tree.transcode(tree.root, value)).encode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise DataError("invalid value encoding") from exc


# ---------------------------------------------------------------------------
# Public export binding
# ---------------------------------------------------------------------------

class ProjectionStore:
    def __init__(self, db, catalog, archive, jobs, results):
        self.db, self.catalog, self.archive = db, catalog, archive
        self.jobs, self.results = jobs, results

    @staticmethod
    def _meta(row):
        return {"export_id": row["export_id"], "job_id": row["job_id"],
                "file_id": row["file_id"], "writer_revision": row["writer_revision"],
                "row_count": row["row_count"], "receipt": row["receipt"]}

    def _find(self, export_id):
        return self.db.conn.execute(
            "SELECT * FROM projection_exports WHERE export_id=?", (export_id,)).fetchone()

    def _allocate_file(self, export_id):
        """Pick a projection file identity that no caller-owned file uses.

        The deterministic identity is preferred; a rare caller file with the
        same name gets a suffixed alternative instead of being overwritten.
        """
        base = "pfile-" + digest(["projection-file", export_id])
        candidate, suffix = base, 1
        while self.db.conn.execute("SELECT 1 FROM files WHERE file_id=?",
                                   (candidate,)).fetchone():
            candidate = base + "-" + str(suffix)
            suffix += 1
        return candidate

    def _source_tagged(self, job):
        """Recompute the fixed job's rows with their original reader union tags."""
        reader_doc = self.catalog.get(job["reader_revision"])
        reader_tree = sdk1.Schema(reader_doc)
        writers = {}
        rows = []
        for occurrence, file_id in enumerate(job["file_ids"]):
            meta = self.archive.metadata(file_id)
            revision = meta["writer_revision"]
            writer = writers.get(revision)
            if writer is None:
                writer_doc = self.catalog.get(revision)
                writer = (writer_doc, sdk1.Schema(writer_doc))
                writers[revision] = writer
            writer_doc, writer_tree = writer
            _, records = self.archive.load(file_id)
            for row_index, record in enumerate(records):
                try:
                    tagged = tagged_resolve(writer_doc, reader_doc, record,
                                            writer_tree, reader_tree)
                except ResolutionError as exc:
                    raise ResolutionError(
                        f"input {occurrence} file {file_id} row {row_index}: {exc}") from exc
                rows.append(((file_id, occurrence, row_index), tagged))
        return reader_tree, rows

    def export_projection(self, job_id, selection):
        identity(job_id)
        job = self.jobs.get(job_id)
        if job["status"] != "succeeded":
            raise DataError("only succeeded jobs can be exported: " + job_id)
        reader_doc = self.catalog.get(job["reader_revision"])
        reader_tree = sdk1.Schema(reader_doc)
        root_node = reader_tree.deref(reader_tree.root)
        if not isinstance(root_node, dict) or root_node.get("type") != "record":
            raise DataError("source reader root is not a record")
        plan = compile_selection(selection, reader_tree, root_node["name"])

        selection_doc = canonical(selection)
        export_id = "pexport-" + digest(
            ["projection-export", job_id, selection_doc])

        existing = self._find(export_id)
        if existing is not None:
            return self._meta(existing)
        file_id = self._allocate_file(export_id)

        source = self.results.read_result(job_id)
        reader_tree, tagged_rows = self._source_tagged(job)
        if len(tagged_rows) != len(source["row_ids"]):
            raise DataError("recomputed row count differs from the fixed result")
        row_ids = []
        for index, ((file_id_seen, occurrence, row_index), tagged) in enumerate(tagged_rows):
            logical = strip_tags(reader_tree, reader_tree.root, tagged)
            if [[file_id_seen, occurrence, row_index]] != [source["row_ids"][index]]:
                raise DataError("recomputed source coordinates differ from the fixed result")
            if logical != source["rows"][index]:
                raise DataError("recomputed row differs from the fixed result")
            row_ids.append([file_id_seen, occurrence, row_index])

        output_schema = SchemaBuilder(reader_tree, plan).build()
        output_tree = sdk1.Schema(output_schema)
        projector = Projector(reader_tree, plan)
        payloads = [_encode(output_tree, projector.row(tagged))
                    for _, tagged in tagged_rows]
        document = canonical(output_schema)

        with self.db.transaction("publish") as conn:
            existing = conn.execute(
                "SELECT * FROM projection_exports WHERE export_id=?", (export_id,)).fetchone()
            if existing is not None:
                return self._meta(existing)
            if conn.execute("SELECT 1 FROM files WHERE file_id=?", (file_id,)).fetchone():
                raise Conflict("projection file identity already exists: " + file_id)
            cur = conn.execute(
                "INSERT INTO schema_revisions(name,document,digest) VALUES(?,?,?)",
                ("projection:" + export_id, document, digest(output_schema)))
            writer_revision = cur.lastrowid
            fingerprint = digest([writer_revision, [payload.hex() for payload in payloads]])
            file_receipt = digest(["file", file_id, fingerprint])
            conn.execute("INSERT INTO files VALUES(?,?,?,?,?)",
                         (file_id, writer_revision, len(payloads), fingerprint, file_receipt))
            conn.executemany("INSERT INTO file_rows VALUES(?,?,?)",
                             [(file_id, index, payload)
                              for index, payload in enumerate(payloads)])
            receipt = digest(["projection", export_id, job_id, job["reader_revision"],
                              writer_revision, file_id, fingerprint, selection_doc,
                              source["receipt"], row_ids])
            conn.execute(
                "INSERT INTO projection_exports VALUES(?,?,?,?,?,?,?,?,?)",
                (export_id, job_id, job["reader_revision"], writer_revision, file_id,
                 selection_doc, len(payloads), receipt, source["receipt"]))
            conn.executemany("INSERT INTO projection_rows VALUES(?,?,?,?,?)",
                             [(export_id, index, file_id_seen, occurrence, row_index)
                              for index, (file_id_seen, occurrence, row_index)
                              in enumerate(row_ids)])
        return {"export_id": export_id, "job_id": job_id, "file_id": file_id,
                "writer_revision": writer_revision, "row_count": len(payloads),
                "receipt": receipt}

    def read_projection(self, export_id):
        identity(export_id)
        row = self._find(export_id)
        if row is None:
            raise NotFound("unknown projection export: " + export_id)
        schema = self.catalog.get(row["writer_revision"])
        tree = sdk1.Schema(schema)
        payloads = self.db.conn.execute(
            "SELECT payload FROM file_rows WHERE file_id=? ORDER BY ordinal",
            (row["file_id"],)).fetchall()
        records = [sdk1.decode(schema, payload["payload"]) for payload in payloads]
        rows = [strip_tags(tree, tree.root, record) for record in records]
        coordinates = self.db.conn.execute(
            "SELECT source_file_id,input_index,row_index FROM projection_rows "
            "WHERE export_id=? ORDER BY ordinal", (export_id,)).fetchall()
        return {**self._meta(row), "schema": schema, "rows": rows,
                "row_ids": [[item["source_file_id"], item["input_index"], item["row_index"]]
                            for item in coordinates]}
