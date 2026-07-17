import os
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class LocalStartupScriptTests(unittest.TestCase):
    def test_use_local_overrides_hosted_environment_flags(self):
        environment = os.environ.copy()
        environment.update(
            {
                "DATABASE_URL": "postgresql://hosted.example/bidlens",
                "AUTO_CREATE_SCHEMA": "false",
                "ENABLE_INTERNAL_SCHEDULER": "false",
                "BIDLENS_VALIDATE_DEPLOYMENT": "true",
                "SESSION_COOKIE_SECURE": "true",
            }
        )

        result = subprocess.run(
            [
                "bash",
                "-c",
                "source scripts/use-local.sh >/dev/null && "
                "PYTHONPATH=src .venv/bin/python -c \""
                "import bidlens.config as config; "
                "print(config.DATABASE_URL); "
                "print(config.AUTO_CREATE_SCHEMA); "
                "print(config.ENABLE_INTERNAL_SCHEDULER); "
                "print(config.VALIDATE_DEPLOYMENT_CONFIG); "
                "print(config.SESSION_COOKIE_SECURE)"
                "\"",
            ],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            ["sqlite:///./bidlens.db", "True", "True", "False", "False"],
        )


if __name__ == "__main__":
    unittest.main()
