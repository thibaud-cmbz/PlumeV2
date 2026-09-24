from datetime import datetime

import pytest
from plume_core.asof import AsOf


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        AsOf(datetime(2026, 1, 15))
