"""Run the complete public v1 business flow and a recursive projection export.

A temporary SQLite file is used so the demo can close and reopen the archive,
proving the published projection is self-contained on disk.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from . import Lake, FailOnce, ResolutionError
from .samples import seed


# Two-state projection over the recursive Link chain: keep every node but
# alternate which fields survive.  The synthesized schema must stay closed
# under recursion even though no row sample drives the definition.
PROJECTION = {"root": "keep", "states": {
    "keep": {"record": "Link", "fields": {
        "value": {}, "next": {"Link": "drop"}, "score": {}, "label": {}}},
    "drop": {"record": "Link", "fields": {
        "next": {"Link": "keep"}}}}}


def main():
    with TemporaryDirectory(prefix="lake-demo-") as directory:
        path = Path(directory) / "demo.sqlite"
        with Lake(path) as lake:
            identities = seed(lake)
            before = lake.read_result("pending")
            lake.db.failpoint = FailOnce("publish.before_commit")
            try:
                lake.run_job("pending")
            except OSError:
                assert lake.read_result("pending") == before
            else:
                raise AssertionError("failure boundary was not reached")
            lake.db.failpoint = None
            result = lake.run_job("pending")
            try:
                lake.run_job("rejected")
            except ResolutionError:
                rejected = lake.read_result("rejected")
            else:
                raise AssertionError("strict enum task should fail")
            assert rejected["rows"] == [] and rejected["status"] == "failed"
            assert result["reader_revision"] == identities["reader"]

            source = lake.read_result("history")
            meta = lake.export_projection("history", PROJECTION)
            exported = lake.read_projection(meta["export_id"])
            tagged = lake.read_file(meta["file_id"])
            assert exported["schema"] == lake.catalog.get(meta["writer_revision"])
            assert exported["row_ids"] == source["row_ids"]
            assert lake.read_result("history") == source
            first = {"fixed_task": result, "failed_task": rejected,
                     "current_reader": identities["latest_reader"],
                     "projection_meta": meta,
                     "projection_rows": exported["rows"],
                     "projection_schema": exported["schema"],
                     "projection_tagged_records": tagged["records"]}
        # Close the connection, then read the published export back from disk.
        with Lake(path) as lake:
            reopened = lake.read_projection(meta["export_id"])
            assert reopened == exported
            assert lake.export_projection("history", PROJECTION) == meta
            first["reopened_projection"] = reopened
            first["reopen_replay_identical"] = True
        print(json.dumps(first, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
