from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # Le marqueur découle du dossier : un test ne peut pas échapper à `-m unit` et `-m integration`.
    for item in items:
        kind = item.path.relative_to(TESTS_DIR).parts[0]
        item.add_marker(getattr(pytest.mark, kind))
