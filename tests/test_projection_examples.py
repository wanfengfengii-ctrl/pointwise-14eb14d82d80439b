"""Run the public projection_examples.verify flow inside the unified regression."""
import contextlib
import io
import unittest

import projection_examples


class TestPublicProjectionExamples(unittest.TestCase):
    def test_verify_script_runs_end_to_end(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            projection_examples.verify()
        self.assertIn("reopened_projection", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
