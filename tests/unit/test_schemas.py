from typing import cast

import pytest
from plume_core.models import PydanticJSON, Recommendation
from plume_core.schemas import RecommendationReason
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

REASONS = cast(PydanticJSON, Recommendation.__table__.c.reasons.type)
DIALECT = postgresql.dialect()  # type: ignore[no-untyped-call]


def test_reasons_round_trip_through_json() -> None:
    reason = RecommendationReason(signal="growth", value=2.5, weight=0.4, sentence="En hausse")

    stored = REASONS.process_bind_param([reason], DIALECT)

    assert stored == [{"signal": "growth", "value": 2.5, "weight": 0.4, "sentence": "En hausse"}]
    assert REASONS.process_result_value(stored, DIALECT) == [reason]


def test_invalid_reasons_are_rejected_before_write() -> None:
    with pytest.raises(ValidationError):
        REASONS.process_bind_param([{"signal": "growth"}], DIALECT)
