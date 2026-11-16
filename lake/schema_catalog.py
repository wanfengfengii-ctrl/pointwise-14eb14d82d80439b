import json
from vendor import sdk1
from .db import canonical, digest, identity
from .errors import NotFound


class SchemaCatalog:
    def __init__(self, db):
        self.db = db

    def register_schema(self, name, schema):
        identity(name)
        checked = sdk1.Schema(schema)
        document = canonical(checked.root)
        with self.db.transaction("catalog") as conn:
            cur = conn.execute(
                "INSERT INTO schema_revisions(name,document,digest) VALUES(?,?,?)",
                (name, document, digest(checked.root)))
            revision = cur.lastrowid
            conn.execute("""INSERT INTO schema_heads VALUES(?,?)
                         ON CONFLICT(name) DO UPDATE SET revision=excluded.revision""",
                         (name, revision))
        return revision

    def get(self, revision):
        if type(revision) is not int:
            raise NotFound("schema revision must be an integer")
        row = self.db.conn.execute(
            "SELECT document FROM schema_revisions WHERE revision=?", (revision,)).fetchone()
        if row is None:
            raise NotFound("unknown schema revision: " + str(revision))
        return json.loads(row["document"])

    def head(self, name):
        row = self.db.conn.execute(
            "SELECT revision FROM schema_heads WHERE name=?", (identity(name),)).fetchone()
        if row is None:
            raise NotFound("unknown schema name: " + name)
        return row["revision"]
