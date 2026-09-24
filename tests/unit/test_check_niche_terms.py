from pathlib import Path

import check_niche_terms
from check_niche_terms import find_violations

TERMS = ["zeppelin", "blimp"]


def write(root: Path, relative_path: str, content: str | bytes) -> str:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return relative_path


def test_detects_term_case_insensitively_with_location(tmp_path: Path) -> None:
    files = [write(tmp_path, "src/a.py", "x = 1\nTOPIC = 'Zeppelin'\n")]

    assert find_violations(tmp_path, files, TERMS) == ["src/a.py:2: Zeppelin"]


def test_underscore_and_hyphen_are_separators(tmp_path: Path) -> None:
    files = [write(tmp_path, "a.py", "zeppelin_score = 1\nblimp-topics\n")]

    assert len(find_violations(tmp_path, files, TERMS)) == 2


def test_substring_is_not_a_match(tmp_path: Path) -> None:
    files = [write(tmp_path, "a.py", "zeppelins = 1\nsubblimp = 2\n")]

    assert find_violations(tmp_path, files, TERMS) == []


def test_fixtures_and_excluded_files_are_ignored(tmp_path: Path) -> None:
    files = [
        write(tmp_path, "fixtures/data.json", '{"topic": "zeppelin"}'),
        write(tmp_path, "scripts/niche_terms.txt", "zeppelin\n"),
    ]

    assert find_violations(tmp_path, files, TERMS) == []


def test_binary_and_missing_files_are_skipped(tmp_path: Path) -> None:
    files = [write(tmp_path, "img.bin", b"\xff\xfezeppelin"), "deleted.py"]

    assert find_violations(tmp_path, files, TERMS) == []


def test_repository_is_clean() -> None:
    assert check_niche_terms.main() == 0
