"""Contrats des champs JSON stockés en base."""

from pydantic import BaseModel, PositiveInt, RootModel


class RecommendationReason(BaseModel):
    signal: str
    value: float
    weight: float
    sentence: str  # phrase affichée à l'utilisateur


class TargetDurations(RootModel[dict[str, PositiveInt]]):
    """Durée cible en secondes, par plateforme (texte libre)."""
