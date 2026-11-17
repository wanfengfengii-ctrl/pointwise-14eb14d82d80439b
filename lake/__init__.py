"""Public schema-bound archive and materialization API."""
from .db import Database, FailOnce
from .errors import LakeError, SchemaError, DataError, ResolutionError, NotFound, Conflict
from .schema_catalog import SchemaCatalog
from .file_archive import FileArchive
from .job_registry import JobRegistry
from .result_store import ResultStore
from .materializer import Materializer
from .projection import ProjectionStore


class Lake:
    def __init__(self, path=":memory:", failpoint=None):
        self.db = Database(path, failpoint)
        self.catalog = SchemaCatalog(self.db)
        self.archive = FileArchive(self.db, self.catalog)
        self.jobs = JobRegistry(self.db, self.catalog, self.archive)
        self.results = ResultStore(self.db, self.jobs)
        self.materializer = Materializer(self.db, self.catalog, self.archive, self.jobs, self.results)
        self.projections = ProjectionStore(self.db, self.catalog, self.archive,
                                           self.jobs, self.results)

    def register_schema(self, name, schema):
        return self.catalog.register_schema(name, schema)

    def append(self, file_id, writer_revision, records):
        return self.archive.append(file_id, writer_revision, records)

    def create_job(self, job_id, file_ids, reader_revision):
        return self.jobs.create_job(job_id, file_ids, reader_revision)

    def run_job(self, job_id):
        return self.materializer.run_job(job_id)

    def read_result(self, job_id):
        return self.results.read_result(job_id)

    def read_file(self, file_id):
        return self.archive.read_file(file_id)

    def export_projection(self, job_id, selection):
        return self.projections.export_projection(job_id, selection)

    def read_projection(self, export_id):
        return self.projections.read_projection(export_id)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
