import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bidlens.db")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
SESSION_COOKIE_NAME = "bidlens_session"
