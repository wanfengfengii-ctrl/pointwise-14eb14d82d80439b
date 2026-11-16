from vendor import sdk1
from .db import digest, identity
from .errors import Conflict, DataError, NotFound


class FileArchive:
    def __init__(self, db, catalog):
        self.db, self.catalog = db, catalog

    def append(self, file_id, writer_revision, records):
        identity(file_id)
        if not isinstance(records, list):
            raise DataError("records must be a complete list")
        schema = self.catalog.get(writer_revision)
        payloads = [sdk1.encode(schema, record) for record in records]
        fingerprint = digest([writer_revision, [p.hex() for p in payloads]])
        receipt = {"file_id": file_id, "writer_revision": writer_revision,
                   "row_count": len(payloads),
                   "receipt": digest(["file", file_id, fingerprint])}
        with self.db.transaction("archive") as conn:
            old = conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
            if old is not None:
                if old["fingerprint"] != fingerprint:
                    raise Conflict("file identity has different content: " + file_id)
                return self._receipt(old)
            conn.execute("INSERT INTO files VALUES(?,?,?,?,?)",
                         (file_id, writer_revision, len(payloads), fingerprint, receipt["receipt"]))
            conn.executemany("INSERT INTO file_rows VALUES(?,?,?)",
                             [(file_id, index, p) for index, p in enumerate(payloads)])
        return receipt

    @staticmethod
    def _receipt(row):
        return {key: row[key] for key in ("file_id", "writer_revision", "row_count", "receipt")}

    def metadata(self, file_id):
        row = self.db.conn.execute("SELECT * FROM files WHERE file_id=?",
                                   (identity(file_id),)).fetchone()
        if row is None:
            raise NotFound("unknown file: " + file_id)
        return self._receipt(row)

    def load(self, file_id):
        meta = self.metadata(file_id)
        schema = self.catalog.get(meta["writer_revision"])
        payloads = self.db.conn.execute(
            "SELECT payload FROM file_rows WHERE file_id=? ORDER BY ordinal", (file_id,)).fetchall()
        return schema, [sdk1.decode(schema, row["payload"]) for row in payloads]

    def read_file(self, file_id):
        meta = self.metadata(file_id)
        _, records = self.load(file_id)
        return {**meta, "records": records}
