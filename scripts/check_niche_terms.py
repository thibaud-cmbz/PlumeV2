"""Échoue si un terme de niche (scripts/niche_terms.txt) apparaît hors de fixtures/."""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TERMS_FILE = ROOT / "scripts" / "niche_terms.txt"
EXCLUDED_DIRS = ("fixtures/",)
EXCLUDED_FILES = frozenset(
    {
        "scripts/niche_terms.txt",
        "scripts/check_niche_terms.py",
        "tests/unit/test_check_niche_terms.py",
    }
)


def load_terms(path: Path) -> list[str]:
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line.lower() for line in lines if line and not line.startswith("#")]


def build_pattern(terms: list[str]) -> re.Pattern[str]:
    # `_` et `-` séparent les mots : `aviation_score` est détecté, `constraint` ne l'est pas.
    # ponytail: le camelCase (`AviationClient`) échappe à la détection ; découper les
    # identifiants si des noms camelCase apparaissent.
    alternatives = "|".join(re.escape(term) for term in terms)
    return re.compile(rf"(?<![a-z0-9])({alternatives})(?![a-z0-9])", re.IGNORECASE)


def list_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def is_excluded(relative_path: str) -> bool:
    return relative_path in EXCLUDED_FILES or relative_path.startswith(EXCLUDED_DIRS)


def find_violations(root: Path, files: list[str], terms: list[str]) -> list[str]:
    pattern = build_pattern(terms)
    violations: list[str] = []
    for relative_path in files:
        path = root / relative_path
        if is_excluded(relative_path) or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for match in pattern.finditer(line):
                violations.append(f"{relative_path}:{number}: {match.group(1)}")
    return violations


def main() -> int:
    violations = find_violations(ROOT, list_files(ROOT), load_terms(TERMS_FILE))
    for violation in violations:
        print(violation)
    if violations:
        print(f"{len(violations)} terme(s) de niche hors de fixtures/.", file=sys.stderr)
        return 1
    print("Aucun terme de niche hors de fixtures/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
