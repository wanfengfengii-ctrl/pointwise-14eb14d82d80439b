import json
from .db import canonical, digest


class ResultStore:
    def __init__(self, db, jobs):
        self.db, self.jobs = db, jobs

    def publish(self, job, candidates, error=None):
        """Called inside materializer's transaction; no independent commit."""
        status = "failed" if error is not None else "succeeded"
        if error is not None:
            candidates = []
        receipt = digest(["result", job["job_id"], job["reader_revision"],
                          job["file_ids"], status, error, candidates])
        conn = self.db.conn
        if not conn.in_transaction:
            raise RuntimeError("publication requires the job transaction")
        conn.execute("DELETE FROM results WHERE job_id=?", (job["job_id"],))
        conn.executemany("INSERT INTO results VALUES(?,?,?,?,?,?)",
                         [(job["job_id"], index, file_id, occurrence, row_index, canonical(record))
                          for index, (file_id, occurrence, row_index, record) in enumerate(candidates)])
        conn.execute("UPDATE jobs SET status=?,error=?,receipt=? WHERE job_id=?",
                     (status, error, receipt, job["job_id"]))

    def read_result(self, job_id):
        job = self.jobs.get(job_id)
        rows = self.db.conn.execute(
            "SELECT * FROM results WHERE job_id=? ORDER BY ordinal", (job_id,)).fetchall()
        return {"job_id": job_id, "reader_revision": job["reader_revision"],
                "status": job["status"], "error": job["error"], "receipt": job["receipt"],
                "rows": [json.loads(row["document"]) for row in rows],
                "row_ids": [[row["file_id"], row["input_index"], row["row_index"]] for row in rows]}
