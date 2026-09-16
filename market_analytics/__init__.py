"""Fundação quantitativa de regimes de mercado (fatia 1).

Este pacote é intencionalmente independente de `core/` (infraestrutura de
terminais, workers e QWebChannel). Ele não conhece MT5, Qt nem GUI: recebe
barras já fechadas e calcula features determinísticas usando somente a
biblioteca padrão.

Fronteira arquitetural: ver `docs/MARKET_ANALYTICS.md`.
"""

from .atlas_contract import (
    HUB_ATLAS_CONTRACT_VERSION,
    to_fusion_quant_v1,
    to_hub_contract,
)
from .atlas_contract import (
    SCHEMA_CHECKPOINT_ROWS as ATLAS_SCHEMA_CHECKPOINT_ROWS,
)
from .atlas_contract import (
    SCHEMA_EVENT_ROWS as ATLAS_SCHEMA_EVENT_ROWS,
)
from .atlas_contract import (
    SCHEMA_OUTSIDE_CORE_ROWS as ATLAS_SCHEMA_OUTSIDE_CORE_ROWS,
)
from .atlas_contract import (
    SCHEMA_QUALITY_ISSUE_ROWS as ATLAS_SCHEMA_QUALITY_ISSUE_ROWS,
)
from .atlas_contract import (
    SCHEMA_SESSION_ROWS as ATLAS_SCHEMA_SESSION_ROWS,
)
from .backfill_catalog import CatalogStateError, list_running_sessions, new_attempt_id, open_catalog
from .backfill_runner import (
    BackfillSessionResult,
    BackfillSourceError,
    advance_backfill_job,
    interrupt_backfill_job,
    run_session_backfill,
    start_backfill_job,
)
from .backfill_writer import (
    BackfillWriteError,
    InspectedFile,
    PromotedFile,
    SessionTickWriter,
    inspect_final_file,
    recompute_summary_from_file,
)
from .bars import Bar, VolumeQuality
from .causal_atlas import (
    AtlasBar,
    AtlasManifest,
    CausalAtlasError,
    CausalAtlasResult,
    build_causal_atlas,
    wrap_bars,
)
from .config import FeatureConfig
from .features import FEATURE_SCHEMA_VERSION, FeatureRow
from .pipeline import compute_feature_rows
from .storage import load_feature_artifact, save_feature_artifact
from .tick_backfill import (
    BACKFILL_COLLECTOR_VERSION,
    BACKFILL_FAILURE_REASONS,
    BACKFILL_SESSION_STATES,
    RAW_SCHEMA_VERSION,
    SESSION_TIMEZONE,
    ArtifactIdentity,
    ArtifactIdentityError,
    BackfillSessionRequest,
    SessionWindow,
    catalog_db_path,
    raw_partition_dir,
    session_window_utc,
    validate_artifact_identity,
)
from .tick_diagnostics import (
    CANONICAL_TICK_TYPES,
    EMPTY_REASON_NO_TICKS,
    FAILURE_REASONS,
    TickRecord,
    TickWindow,
    TickWindowAccumulator,
    TickWindowRequest,
    TickWindowSummary,
    mt5_tick_type_attr,
    validate_tick_record,
)

__all__ = [
    "Bar",
    "VolumeQuality",
    "AtlasBar",
    "AtlasManifest",
    "CausalAtlasError",
    "CausalAtlasResult",
    "build_causal_atlas",
    "wrap_bars",
    "HUB_ATLAS_CONTRACT_VERSION",
    "ATLAS_SCHEMA_SESSION_ROWS",
    "ATLAS_SCHEMA_CHECKPOINT_ROWS",
    "ATLAS_SCHEMA_EVENT_ROWS",
    "ATLAS_SCHEMA_OUTSIDE_CORE_ROWS",
    "ATLAS_SCHEMA_QUALITY_ISSUE_ROWS",
    "to_fusion_quant_v1",
    "to_hub_contract",
    "FeatureConfig",
    "FeatureRow",
    "FEATURE_SCHEMA_VERSION",
    "compute_feature_rows",
    "save_feature_artifact",
    "load_feature_artifact",
    "CANONICAL_TICK_TYPES",
    "EMPTY_REASON_NO_TICKS",
    "FAILURE_REASONS",
    "TickRecord",
    "TickWindow",
    "TickWindowAccumulator",
    "TickWindowRequest",
    "TickWindowSummary",
    "mt5_tick_type_attr",
    "validate_tick_record",
    "BACKFILL_COLLECTOR_VERSION",
    "BACKFILL_FAILURE_REASONS",
    "BACKFILL_SESSION_STATES",
    "RAW_SCHEMA_VERSION",
    "SESSION_TIMEZONE",
    "BackfillSessionRequest",
    "SessionWindow",
    "ArtifactIdentity",
    "ArtifactIdentityError",
    "validate_artifact_identity",
    "catalog_db_path",
    "raw_partition_dir",
    "session_window_utc",
    "BackfillWriteError",
    "InspectedFile",
    "PromotedFile",
    "SessionTickWriter",
    "inspect_final_file",
    "recompute_summary_from_file",
    "CatalogStateError",
    "new_attempt_id",
    "list_running_sessions",
    "open_catalog",
    "BackfillSessionResult",
    "BackfillSourceError",
    "advance_backfill_job",
    "interrupt_backfill_job",
    "run_session_backfill",
    "start_backfill_job",
]
