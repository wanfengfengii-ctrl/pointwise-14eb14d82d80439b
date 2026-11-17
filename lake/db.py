"""SQLite transaction and deterministic failure boundary used by all owners."""
from contextlib import contextmanager
import hashlib
import json
import sqlite3
from .errors import DataError

DDL = (
    "CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS schema_revisions(
       revision INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,
       document TEXT NOT NULL,digest TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS schema_heads(
       name TEXT PRIMARY KEY,revision INTEGER NOT NULL REFERENCES schema_revisions)""",
    """CREATE TABLE IF NOT EXISTS files(
       file_id TEXT PRIMARY KEY,writer_revision INTEGER NOT NULL REFERENCES schema_revisions,
       row_count INTEGER NOT NULL,fingerprint TEXT NOT NULL,receipt TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS file_rows(
       file_id TEXT NOT NULL REFERENCES files,ordinal INTEGER NOT NULL,payload BLOB NOT NULL,
       PRIMARY KEY(file_id,ordinal))""",
    """CREATE TABLE IF NOT EXISTS jobs(
       job_id TEXT PRIMARY KEY,reader_revision INTEGER NOT NULL REFERENCES schema_revisions,
       file_ids TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('queued','succeeded','failed')),
       error TEXT,receipt TEXT)""",
    """CREATE TABLE IF NOT EXISTS results(
       job_id TEXT NOT NULL REFERENCES jobs,ordinal INTEGER NOT NULL,
       file_id TEXT NOT NULL REFERENCES files,input_index INTEGER NOT NULL,
       row_index INTEGER NOT NULL,document TEXT NOT NULL,PRIMARY KEY(job_id,ordinal))""",
    """CREATE TABLE IF NOT EXISTS projection_exports(
       export_id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs,
       reader_revision INTEGER NOT NULL REFERENCES schema_revisions,
       writer_revision INTEGER NOT NULL REFERENCES schema_revisions,
       file_id TEXT NOT NULL REFERENCES files,selection TEXT NOT NULL,
       row_count INTEGER NOT NULL,receipt TEXT NOT NULL,source_receipt TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS projection_rows(
       export_id TEXT NOT NULL REFERENCES projection_exports,ordinal INTEGER NOT NULL,
       source_file_id TEXT NOT NULL,input_index INTEGER NOT NULL,row_index INTEGER NOT NULL,
       PRIMARY KEY(export_id,ordinal))""",
)


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise DataError("value must be finite JSON") from exc


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def identity(value):
    if not isinstance(value, str) or not value:
        raise DataError("identity must be a nonempty string")
    return value


class FailOnce:
    """Raise once at an exact transaction checkpoint, before COMMIT."""
    def __init__(self, stage):
        self.stage = stage
        self.fired = False

    def __call__(self, stage):
        if stage == self.stage and not self.fired:
            self.fired = True
            raise OSError("injected failure at " + stage)


class Database:
    def __init__(self, path=":memory:", failpoint=None):
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.failpoint = failpoint
        try:
            with self.transaction("initialize"):
                for statement in DDL:
                    self.conn.execute(statement)
                self.conn.execute("INSERT OR IGNORE INTO metadata VALUES('storage_version','1')")
        except BaseException:
            self.conn.close()
            raise

    def checkpoint(self, stage):
        if self.failpoint is not None:
            self.failpoint(stage)

    @contextmanager
    def transaction(self, label):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.checkpoint(label + ".before_commit")
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def close(self):
        self.conn.close()
