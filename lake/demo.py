"""Run the complete public v1 business flow without writing a database file."""
import json
from . import Lake, FailOnce, ResolutionError
from .samples import seed


def main():
    with Lake() as lake:
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
        print(json.dumps({"fixed_task": result, "failed_task": rejected,
                          "history": lake.read_result("history"),
                          "current_reader": identities["latest_reader"]},
                         ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
