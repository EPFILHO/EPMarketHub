"""Cobertura do registro de manifestos conhecidos (DEV-004 — C3.1).

Único manifesto aprovado nesta ordem: `c3-win-clear`. Um `manifest_id`
desconhecido (por exemplo um WDO hipotético) nunca resolve para um arquivo.
"""

from __future__ import annotations

from datetime import date, time

import pytest

from market_analytics.manifest import BackfillManifest, ManifestAsset, save_manifest_file
from market_analytics.manifest_registry import (
    KNOWN_MANIFESTS,
    MANIFESTS_DIR,
    ManifestRegistryIntegrityError,
    UnknownManifestError,
    known_manifest_ids,
    load_known_manifest,
)


def test_registry_only_knows_c3_win_clear() -> None:
    assert known_manifest_ids() == ("c3-win-clear",)
    assert set(KNOWN_MANIFESTS) == {"c3-win-clear"}


def test_registry_paths_point_inside_the_manifests_directory() -> None:
    for path in KNOWN_MANIFESTS.values():
        assert path.parent == MANIFESTS_DIR
        assert path.exists()


def test_load_known_manifest_returns_the_approved_c3_win_manifest() -> None:
    manifest = load_known_manifest("c3-win-clear")
    assert manifest.manifest_id == "c3-win-clear"
    assert manifest.source_id == "clear"
    assert manifest.authorization_state == "planning"


def test_load_known_manifest_id_matches_the_registry_key_for_every_known_manifest() -> None:
    """Achado 4 da auditoria Codex: o id carregado precisa bater exatamente
    com a chave usada para procurá-lo -- não basta o registro apontar para
    *algum* arquivo válido."""

    for manifest_id in known_manifest_ids():
        manifest = load_known_manifest(manifest_id)
        assert manifest.manifest_id == manifest_id


def test_load_known_manifest_rejects_unknown_manifest_id() -> None:
    with pytest.raises(UnknownManifestError):
        load_known_manifest("c3-wdo-clear")


def test_load_known_manifest_rejects_a_registry_entry_whose_file_id_does_not_match(tmp_path, monkeypatch) -> None:
    """Registro mal configurado (arquivo errado, cópia desatualizada): o
    `manifest_id` de dentro do arquivo não bate com a chave `KNOWN_MANIFESTS`
    usada para carregá-lo -- nunca devolvido silenciosamente."""

    mismatched = BackfillManifest(
        manifest_id="real-id",
        display_name="Manifesto fictício de teste",
        work_order="DEV-004",
        source_id="fake_source",
        session_timezone="America/Sao_Paulo",
        session_close_local_time=time(19, 0),
        assets=(
            ManifestAsset(
                logical_id="fake_a",
                requested_symbol="FAKEA",
                provenance=(("series_kind", "continuous"),),
            ),
        ),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 14),
        execution_order="oldest_first",
        chunk_seconds=900,
        max_attempts=3,
        concurrency=1,
        max_total_bytes=1_000_000_000,
        max_total_duration_seconds=3_600,
        authorization_state="planning",
    )
    path = tmp_path / "mismatched.json"
    save_manifest_file(path, mismatched)
    monkeypatch.setitem(KNOWN_MANIFESTS, "wrong-key", path)

    with pytest.raises(ManifestRegistryIntegrityError):
        load_known_manifest("wrong-key")
