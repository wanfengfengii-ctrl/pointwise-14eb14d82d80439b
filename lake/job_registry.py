import json
from .db import canonical, identity
from .errors import Conflict, DataError, NotFound


class JobRegistry:
    def __init__(self, db, catalog, archive):
        self.db, self.catalog, self.archive = db, catalog, archive

    def create_job(self, job_id, file_ids, reader_revision):
        identity(job_id)
        if not isinstance(file_ids, list):
            raise DataError("file_ids must be a complete list")
        self.catalog.get(reader_revision)
        for file_id in file_ids:
            self.archive.metadata(file_id)
        encoded = canonical(file_ids)
        with self.db.transaction("job") as conn:
            old = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if old is not None:
                if old["reader_revision"] != reader_revision or old["file_ids"] != encoded:
                    raise Conflict("job identity has different bindings: " + job_id)
            else:
                conn.execute("INSERT INTO jobs VALUES(?,?,?,'queued',NULL,NULL)",
                             (job_id, reader_revision, encoded))
        return self.get(job_id)

    def get(self, job_id):
        row = self.db.conn.execute("SELECT * FROM jobs WHERE job_id=?",
                                   (identity(job_id),)).fetchone()
        if row is None:
            raise NotFound("unknown job: " + job_id)
        return {**dict(row), "file_ids": json.loads(row["file_ids"])}
