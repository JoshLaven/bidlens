import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.bidlens.config import DOTENV_PATH, SAM_API_KEY


def mask(value: str | None) -> str:
    if not value:
        return "(missing)"

    value = str(value)
    if len(value) <= 10:
        return value[:2] + "***"
    return value[:6] + "***" + value[-4:]


def main() -> None:
    print(f".env path: {DOTENV_PATH}")
    print(f"SAM_API_KEY present: {'yes' if bool(SAM_API_KEY) else 'no'}")
    print(f"SAM_API_KEY value: {mask(SAM_API_KEY)}")


if __name__ == "__main__":
    main()
