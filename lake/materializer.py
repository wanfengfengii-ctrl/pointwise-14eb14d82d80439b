from vendor import sdk1
from .errors import ResolutionError


class Materializer:
    def __init__(self, db, catalog, archive, jobs, results):
        self.db, self.catalog, self.archive = db, catalog, archive
        self.jobs, self.results = jobs, results

    def run_job(self, job_id):
        failure = None
        with self.db.transaction("publish"):
            job = self.jobs.get(job_id)
            if job["status"] == "failed":
                failure = job["error"]
            elif job["status"] == "queued":
                reader = self.catalog.get(job["reader_revision"])
                candidates = []
                try:
                    for occurrence, file_id in enumerate(job["file_ids"]):
                        writer, records = self.archive.load(file_id)
                        for row_index, record in enumerate(records):
                            try:
                                resolved = sdk1.resolve(writer, reader, record)
                            except ResolutionError as exc:
                                raise ResolutionError(
                                    f"input {occurrence} file {file_id} row {row_index}: {exc}") from exc
                            candidates.append((file_id, occurrence, row_index, resolved))
                except ResolutionError as exc:
                    failure = str(exc)
                self.results.publish(job, candidates, failure)
        if failure is not None:
            raise ResolutionError(failure)
        return self.results.read_result(job_id)
