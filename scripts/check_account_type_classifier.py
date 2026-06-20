import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bidlens.services.account_type_classifier import classify_account_type


CASES = {
    "Department of Health and Human Services": "Federal",
    "Centers for Medicare & Medicaid Services": "Federal",
    "Arizona Department of Health Services": "State Government",
    "State of California Department of Education": "State Government",
    "Maricopa County": "Regional Government",
    "AUSTIN, CITY OF (TRAVIS)": "Regional Government",
    "Phoenix Union High School District": "Regional Government",
    "University of Arizona": "Nonprofit University",
    "Arizona Board of Regents": "Nonprofit University",
}


def main() -> None:
    for name, expected in CASES.items():
        actual = classify_account_type(name).account_type
        assert actual == expected, f"{name!r}: expected {expected!r}, got {actual!r}"
    print(f"{len(CASES)} account type classifier sample cases passed")


if __name__ == "__main__":
    main()
