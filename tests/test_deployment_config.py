import unittest

from bidlens.config import DeploymentConfigError, startup_diagnostics, validate_deployment_config


class DeploymentConfigValidationTests(unittest.TestCase):
    def test_local_dev_defaults_do_not_require_hosted_settings(self):
        validate_deployment_config(
            raw_database_url=None,
            database_url="sqlite:///./bidlens.db",
            database_scheme="sqlite",
            secret_key="dev-secret-key-change-in-production",
            session_cookie_secure=False,
            auto_create_schema=True,
            enable_internal_scheduler=True,
            explicit_validate=False,
        )

    def test_hosted_rejects_missing_database_url(self):
        with self.assertRaisesRegex(DeploymentConfigError, "DATABASE_URL is required"):
            validate_deployment_config(
                raw_database_url="",
                database_url="sqlite:///./bidlens.db",
                database_scheme="sqlite",
                secret_key="not-the-default-secret",
                session_cookie_secure=True,
                auto_create_schema=False,
                enable_internal_scheduler=False,
            )

    def test_hosted_rejects_default_secret_key(self):
        with self.assertRaisesRegex(DeploymentConfigError, "SECRET_KEY"):
            validate_deployment_config(
                raw_database_url="postgresql://user:secret@example.com:5432/bidlens",
                database_url="postgresql://user:secret@example.com:5432/bidlens",
                database_scheme="postgresql",
                secret_key="dev-secret-key-change-in-production",
                session_cookie_secure=True,
                auto_create_schema=False,
                enable_internal_scheduler=False,
            )

    def test_hosted_rejects_insecure_session_cookie(self):
        with self.assertRaisesRegex(DeploymentConfigError, "SESSION_COOKIE_SECURE"):
            validate_deployment_config(
                raw_database_url="postgresql://user:secret@example.com:5432/bidlens",
                database_url="postgresql://user:secret@example.com:5432/bidlens",
                database_scheme="postgresql",
                secret_key="not-the-default-secret",
                session_cookie_secure=False,
                auto_create_schema=False,
                enable_internal_scheduler=False,
            )

    def test_hosted_accepts_valid_postgres_config(self):
        validate_deployment_config(
            raw_database_url="postgresql://user:secret@example.com:5432/bidlens",
            database_url="postgresql://user:secret@example.com:5432/bidlens",
            database_scheme="postgresql",
            secret_key="not-the-default-secret",
            session_cookie_secure=True,
            auto_create_schema=False,
            enable_internal_scheduler=False,
        )

    def test_validation_errors_and_startup_diagnostics_do_not_expose_secrets(self):
        with self.assertRaises(DeploymentConfigError) as raised:
            validate_deployment_config(
                raw_database_url="postgresql://user:database-password@example.com:5432/bidlens",
                database_url="postgresql://user:database-password@example.com:5432/bidlens",
                database_scheme="postgresql",
                secret_key="application-secret-value",
                session_cookie_secure=False,
                auto_create_schema=False,
                enable_internal_scheduler=True,
            )

        diagnostics = "\n".join(
            startup_diagnostics(
                database_scheme="postgresql",
                auto_create_schema=False,
                enable_internal_scheduler=True,
                session_cookie_secure=False,
            )
        )
        output = f"{raised.exception}\n{diagnostics}"

        self.assertNotIn("database-password", output)
        self.assertNotIn("application-secret-value", output)


if __name__ == "__main__":
    unittest.main()
