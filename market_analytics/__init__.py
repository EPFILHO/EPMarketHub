"""Fundação quantitativa de regimes de mercado (fatia 1).

Este pacote é intencionalmente independente de `core/` (infraestrutura de
terminais, workers e QWebChannel). Ele não conhece MT5, Qt nem GUI: recebe
barras já fechadas e calcula features determinísticas usando somente a
biblioteca padrão.

Fronteira arquitetural: ver `docs/MARKET_ANALYTICS.md`.
"""

from .bars import Bar, VolumeQuality
from .config import FeatureConfig
from .features import FEATURE_SCHEMA_VERSION, FeatureRow
from .pipeline import compute_feature_rows
from .storage import load_feature_artifact, save_feature_artifact

__all__ = [
    "Bar",
    "VolumeQuality",
    "FeatureConfig",
    "FeatureRow",
    "FEATURE_SCHEMA_VERSION",
    "compute_feature_rows",
    "save_feature_artifact",
    "load_feature_artifact",
]
