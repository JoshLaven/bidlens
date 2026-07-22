import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from bidlens.jobs import run_grants_refresh


class GrantsRefreshCommandTests(unittest.TestCase):
    def test_successful_refresh_returns_zero(self):
        with patch("bidlens.jobs.run_grants_refresh._run_operational_job", return_value=0) as run_job:
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = run_grants_refresh.run([])

        self.assertEqual(exit_code, 0)
        run_job.assert_called_once_with(trigger_type="scheduled")
        self.assertIn("BidLens Grants.gov refresh started", output.getvalue())
        self.assertIn("completed successfully", output.getvalue())

    def test_organization_level_failures_do_not_fail_command(self):
        with patch("bidlens.jobs.run_grants_refresh._run_operational_job", return_value=1) as run_job:
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = run_grants_refresh.run([])

        self.assertEqual(exit_code, 0)
        run_job.assert_called_once_with(trigger_type="scheduled")
        self.assertIn("organization-level failures", output.getvalue())

    def test_job_level_failure_returns_nonzero(self):
        with patch("bidlens.jobs.run_grants_refresh._run_operational_job", side_effect=RuntimeError("database unavailable")):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = run_grants_refresh.run([])

        self.assertEqual(exit_code, 1)
        self.assertIn("failed before completion: RuntimeError", output.getvalue())


if __name__ == "__main__":
    unittest.main()
