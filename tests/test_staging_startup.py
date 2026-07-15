import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _drop_modules(*names: str) -> None:
    for name in names:
        sys.modules.pop(name, None)
    package = sys.modules.get("bidlens")
    if package is not None:
        for name in names:
            if name.startswith("bidlens."):
                attr = name.rsplit(".", 1)[1]
                if hasattr(package, attr):
                    delattr(package, attr)


class StagingStartupTests(unittest.TestCase):
    def _hosted_env(self):
        return {
            "DATABASE_URL": "postgresql://user:secret@example.com:5432/bidlens",
            "SECRET_KEY": "test-secret-key-that-is-not-the-default",
            "AUTO_CREATE_SCHEMA": "false",
            "ENABLE_INTERNAL_SCHEDULER": "false",
            "SESSION_COOKIE_SECURE": "true",
        }

    def _fresh_main(self, **env):
        _drop_modules("bidlens.main", "bidlens.config")
        with patch.dict(os.environ, env, clear=False):
            return importlib.import_module("bidlens.main")

    def test_health_endpoint_works_with_staging_startup_flags(self):
        main = self._fresh_main(**self._hosted_env())

        with TestClient(main.app) as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_local_sqlite_startup_still_works_with_dev_defaults(self):
        _drop_modules("bidlens.main", "bidlens.config", "bidlens.database")
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
            sqlite_path = db_file.name

        try:
            main = self._fresh_main(
                DATABASE_URL=f"sqlite:///{sqlite_path}",
                AUTO_CREATE_SCHEMA="true",
                ENABLE_INTERNAL_SCHEDULER="false",
                SESSION_COOKIE_SECURE="false",
                SECRET_KEY="dev-secret-key-change-in-production",
            )

            with TestClient(main.app) as client:
                response = client.get("/health")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": "ok"})
        finally:
            Path(sqlite_path).unlink(missing_ok=True)

    def test_scheduler_does_not_start_when_disabled(self):
        main = self._fresh_main(**self._hosted_env())
        main.start_scheduler = Mock()

        with TestClient(main.app) as client:
            self.assertEqual(client.get("/health").status_code, 200)

        main.start_scheduler.assert_not_called()

    def test_session_cookie_can_be_marked_secure_for_https(self):
        _drop_modules("bidlens.auth", "bidlens.config")
        with patch.dict(os.environ, {"SESSION_COOKIE_SECURE": "true"}, clear=False):
            auth = importlib.import_module("bidlens.auth")

        response = RedirectResponse(url="/home")
        auth.create_session(response, user_id=123)

        self.assertIn("httponly", response.headers["set-cookie"].lower())
        self.assertIn("samesite=lax", response.headers["set-cookie"].lower())
        self.assertIn("secure", response.headers["set-cookie"].lower())


if __name__ == "__main__":
    unittest.main()
