"""Cobertura do manifesto puro e estrito (DEV-004 — C3.1).

Todos os testes usam apenas `tmp_path` e dicionários fabricados à mão; nenhum
teste toca `D:\\EPData`, abre MT5/Clear/FOT nem instancia Tk/Qt. A pureza de
import é verificada num subprocesso isolado (`test_pure_modules_do_not_import_mt5_or_gui`)
para não depender de que nenhum outro teste já tenha importado `tkinter` no
mesmo processo pytest.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from datetime import UTC, date, time
from pathlib import Path

import pytest

from market_analytics.manifest import (
    AUTHORIZATION_STATES,
    BackfillManifest,
    ManifestAsset,
    ManifestValidationError,
    load_manifest_file,
    save_manifest_file,
)

C3_WIN_PATH = Path(__file__).resolve().parents[1] / "market_analytics" / "manifests" / "c3_win_clear.json"


def _valid_manifest_dict() -> dict:
    return {
        "schema": "ep_market_hub.backfill_manifest",
        "schema_version": 1,
        "manifest_id": "c3-win-clear",
        "display_name": "C3-WIN — Clear — WIN$ contínuo (liquidez/proporcional)",
        "work_order": "DEV-003/C3-WIN",
        "source_id": "clear",
        "session_timezone": "America/Sao_Paulo",
        "session_close_local_time": "19:00",
        "assets": [
            {
                "logical_id": "win",
                "requested_symbol": "WIN$",
                "provenance": {
                    "series_kind": "continuous",
                    "instrument": "win",
                    "source_symbol": "WIN$",
                    "roll_rule": "liquidity",
                    "adjustment_method": "proportional",
                    "analytics_roles": "regime,relative_returns,cross_asset",
                },
            }
        ],
        "start_date": "2025-07-14",
        "end_date_policy": "latest_closed",
        "execution_order": "newest_first",
        "chunk_seconds": 900,
        "max_attempts": 3,
        "concurrency": 1,
        "max_total_bytes": 53687091200,
        "max_total_duration_seconds": 172800,
        "authorization_state": "planning",
    }


# --- Manifesto aprovado C3-WIN ---------------------------------------------


def test_c3_win_clear_manifest_loads_and_matches_the_approved_shape() -> None:
    manifest = load_manifest_file(C3_WIN_PATH)

    assert manifest.source_id == "clear"
    assert manifest.manifest_id == "c3-win-clear"
    assert manifest.work_order == "DEV-003/C3-WIN"
    assert [asset.logical_id for asset in manifest.assets] == ["win"]
    assert [asset.requested_symbol for asset in manifest.assets] == ["WIN$"]
    assert manifest.session_close_local_time == time(19, 0)
    assert manifest.start_date == date(2025, 7, 14)
    assert manifest.end_date_policy == "latest_closed"
    assert manifest.end_date is None
    assert manifest.execution_order == "newest_first"
    assert manifest.chunk_seconds == 900
    assert manifest.concurrency == 1
    assert manifest.authorization_state == "planning"
    assert manifest.is_execution_authorized is False


def test_c3_win_clear_manifest_never_mentions_wdo() -> None:
    manifest = load_manifest_file(C3_WIN_PATH)

    logical_ids = {asset.logical_id for asset in manifest.assets}
    requested_symbols = {asset.requested_symbol for asset in manifest.assets}
    assert "wdo" not in logical_ids
    assert not any("WDO" in symbol.upper() for symbol in requested_symbols)


# --- Fingerprint determinístico ---------------------------------------------


def test_fingerprint_is_deterministic_for_the_same_manifest() -> None:
    manifest_a = BackfillManifest(**_manifest_kwargs())
    manifest_b = BackfillManifest(**_manifest_kwargs())

    assert manifest_a.fingerprint() == manifest_b.fingerprint()
    assert len(manifest_a.fingerprint()) == 64


def test_fingerprint_changes_when_any_field_changes() -> None:
    manifest_a = BackfillManifest(**_manifest_kwargs())
    manifest_b = BackfillManifest(**_manifest_kwargs(display_name="Outro nome"))

    assert manifest_a.fingerprint() != manifest_b.fingerprint()


def _manifest_kwargs(**overrides) -> dict:
    base = dict(
        manifest_id="c3-win-clear",
        display_name="C3-WIN — Clear",
        work_order="DEV-003/C3-WIN",
        source_id="clear",
        session_timezone="America/Sao_Paulo",
        session_close_local_time=time(19, 0),
        assets=(
            ManifestAsset(
                logical_id="win",
                requested_symbol="WIN$",
                provenance=(("series_kind", "continuous"), ("instrument", "win")),
            ),
        ),
        start_date=date(2025, 7, 14),
        end_date_policy="latest_closed",
        execution_order="newest_first",
        chunk_seconds=900,
        max_attempts=3,
        concurrency=1,
        max_total_bytes=53_687_091_200,
        max_total_duration_seconds=172_800,
        authorization_state="planning",
    )
    base.update(overrides)
    return base


# --- Validação estrita -------------------------------------------------------


def test_from_dict_rejects_unknown_field() -> None:
    data = _valid_manifest_dict()
    data["account_login"] = "12345"
    with pytest.raises(ManifestValidationError, match="desconhecido"):
        BackfillManifest.from_dict(data)


def test_from_dict_rejects_missing_field() -> None:
    data = _valid_manifest_dict()
    del data["chunk_seconds"]
    with pytest.raises(ManifestValidationError, match="ausente"):
        BackfillManifest.from_dict(data)


def test_from_dict_rejects_duplicate_logical_id() -> None:
    data = _valid_manifest_dict()
    data["assets"].append(copy.deepcopy(data["assets"][0]))
    with pytest.raises(ManifestValidationError, match="duplicado"):
        BackfillManifest.from_dict(data)


def test_from_dict_rejects_empty_assets_list() -> None:
    data = _valid_manifest_dict()
    data["assets"] = []
    with pytest.raises(ManifestValidationError):
        BackfillManifest.from_dict(data)


def test_from_dict_rejects_invalid_timezone() -> None:
    data = _valid_manifest_dict()
    data["session_timezone"] = "Not/AZone"
    with pytest.raises(ManifestValidationError, match="timezone"):
        BackfillManifest.from_dict(data)


@pytest.mark.parametrize("value", ["19:00", "19:00:00", "00:00", "23:59:59"])
def test_from_dict_accepts_valid_session_close_local_time_formats(value: str) -> None:
    data = _valid_manifest_dict()
    data["session_close_local_time"] = value
    manifest = BackfillManifest.from_dict(data)
    assert manifest.session_close_local_time == time.fromisoformat(value)


@pytest.mark.parametrize(
    "value",
    ["9:00", "19:60", "24:00", "19:00:60", "not-a-time", "19h00", "", "19:00:00.5", 1900],
)
def test_from_dict_rejects_invalid_session_close_local_time(value) -> None:
    data = _valid_manifest_dict()
    data["session_close_local_time"] = value
    with pytest.raises(ManifestValidationError, match="session_close_local_time"):
        BackfillManifest.from_dict(data)


def test_from_dict_rejects_missing_session_close_local_time() -> None:
    data = _valid_manifest_dict()
    del data["session_close_local_time"]
    with pytest.raises(ManifestValidationError, match="ausente"):
        BackfillManifest.from_dict(data)


def test_constructor_rejects_session_close_local_time_with_tzinfo() -> None:
    with pytest.raises(ManifestValidationError, match="tzinfo"):
        BackfillManifest(**_manifest_kwargs(session_close_local_time=time(19, 0, tzinfo=UTC)))


def test_constructor_rejects_non_time_session_close_local_time() -> None:
    with pytest.raises(ManifestValidationError):
        BackfillManifest(**_manifest_kwargs(session_close_local_time="19:00"))


def test_from_dict_rejects_inverted_interval() -> None:
    data = _valid_manifest_dict()
    data["end_date_policy"] = "explicit"
    data["start_date"] = "2025-07-14"
    data["end_date"] = "2025-07-01"
    with pytest.raises(ManifestValidationError, match="invertido"):
        BackfillManifest.from_dict(data)


def test_from_dict_rejects_explicit_policy_without_end_date() -> None:
    data = _valid_manifest_dict()
    data["end_date_policy"] = "explicit"
    with pytest.raises(ManifestValidationError):
        BackfillManifest.from_dict(data)


def test_from_dict_rejects_latest_closed_policy_declaring_end_date() -> None:
    data = _valid_manifest_dict()
    data["end_date"] = "2025-08-01"
    with pytest.raises(ManifestValidationError):
        BackfillManifest.from_dict(data)


@pytest.mark.parametrize("chunk_seconds", [59, 901, 0, -1])
def test_from_dict_rejects_chunk_seconds_out_of_range(chunk_seconds: int) -> None:
    data = _valid_manifest_dict()
    data["chunk_seconds"] = chunk_seconds
    with pytest.raises(ManifestValidationError, match="chunk_seconds"):
        BackfillManifest.from_dict(data)


@pytest.mark.parametrize("concurrency", [0, 2, -1])
def test_from_dict_rejects_concurrency_different_from_one(concurrency: int) -> None:
    data = _valid_manifest_dict()
    data["concurrency"] = concurrency
    with pytest.raises(ManifestValidationError, match="concurrency"):
        BackfillManifest.from_dict(data)


@pytest.mark.parametrize("field", ["max_total_bytes", "max_total_duration_seconds"])
@pytest.mark.parametrize("value", [0, -1])
def test_from_dict_rejects_non_positive_limits(field: str, value: int) -> None:
    data = _valid_manifest_dict()
    data[field] = value
    with pytest.raises(ManifestValidationError):
        BackfillManifest.from_dict(data)


def test_from_dict_rejects_explicit_policy_with_end_date_key_present_but_null() -> None:
    """`end_date: null` com `end_date_policy='explicit'` é recusado de forma
    limpa (`ManifestValidationError`), nunca com um `KeyError`/`TypeError`
    não tratado."""

    data = _valid_manifest_dict()
    data["end_date_policy"] = "explicit"
    data["end_date"] = None
    with pytest.raises(ManifestValidationError):
        BackfillManifest.from_dict(data)


def test_asset_from_dict_rejects_missing_field() -> None:
    data = _valid_manifest_dict()
    del data["assets"][0]["provenance"]
    with pytest.raises(ManifestValidationError, match="ausente"):
        BackfillManifest.from_dict(data)


def test_from_dict_rejects_unknown_authorization_state() -> None:
    data = _valid_manifest_dict()
    data["authorization_state"] = "backfilling"
    with pytest.raises(ManifestValidationError):
        BackfillManifest.from_dict(data)


def test_from_dict_rejects_unknown_execution_order() -> None:
    data = _valid_manifest_dict()
    data["execution_order"] = "random"
    with pytest.raises(ManifestValidationError):
        BackfillManifest.from_dict(data)


def test_asset_from_dict_rejects_unknown_field() -> None:
    data = _valid_manifest_dict()
    data["assets"][0]["account"] = "123"
    with pytest.raises(ManifestValidationError, match="desconhecido"):
        BackfillManifest.from_dict(data)


def test_asset_rejects_empty_provenance() -> None:
    data = _valid_manifest_dict()
    data["assets"][0]["provenance"] = {}
    with pytest.raises(ManifestValidationError, match="provenance"):
        BackfillManifest.from_dict(data)


@pytest.mark.parametrize("field", ["account", "login", "password", "credentials", "token", "api_key"])
def test_manifest_rejects_credential_like_fields_as_unknown(field: str) -> None:
    """Credenciais/conta/login não têm onde entrar: o esquema fechado do
    manifesto rejeita esses campos como "desconhecido", nunca os aceita
    silenciosamente."""

    data = _valid_manifest_dict()
    data[field] = "should-never-be-accepted"
    with pytest.raises(ManifestValidationError, match="desconhecido"):
        BackfillManifest.from_dict(data)


@pytest.mark.parametrize("field", ["account", "login", "password", "credentials", "token", "api_key"])
def test_manifest_asset_rejects_credential_like_fields_as_unknown(field: str) -> None:
    data = _valid_manifest_dict()
    data["assets"][0][field] = "should-never-be-accepted"
    with pytest.raises(ManifestValidationError, match="desconhecido"):
        BackfillManifest.from_dict(data)


# --- planning nunca habilita execução ---------------------------------------


@pytest.mark.parametrize("state", sorted(AUTHORIZATION_STATES))
def test_is_execution_authorized_is_true_only_for_execution_approved(state: str) -> None:
    manifest = BackfillManifest(**_manifest_kwargs(authorization_state=state))
    assert manifest.is_execution_authorized == (state == "execution_approved")


# --- round-trip atômico em tmp_path ------------------------------------------


def test_save_and_load_manifest_file_round_trips_in_tmp_path(tmp_path) -> None:
    manifest = BackfillManifest(**_manifest_kwargs())
    path = tmp_path / "nested" / "manifest.json"

    save_manifest_file(path, manifest)
    reloaded = load_manifest_file(path)

    assert reloaded.to_dict() == manifest.to_dict()
    assert reloaded.fingerprint() == manifest.fingerprint()
    # nenhum arquivo temporário sobra ao lado do resultado final
    assert list(path.parent.iterdir()) == [path]


def test_load_manifest_file_rejects_invalid_json(tmp_path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ManifestValidationError):
        load_manifest_file(path)


# --- Pureza de import ---------------------------------------------------------


def test_pure_modules_do_not_import_mt5_or_gui() -> None:
    """Roda num subprocesso isolado: garante que só importar os módulos puros
    do C3.1 nunca carrega `MetaTrader5`, `tkinter` ou Qt como efeito colateral,
    independentemente do que outro teste já tenha importado no processo atual.
    """

    script = (
        "import sys\n"
        "import market_analytics.manifest\n"
        "import market_analytics.manifest_registry\n"
        "import market_analytics.backfill_plan\n"
        "import market_analytics.backfill_adapter\n"
        "forbidden = {'MetaTrader5', 'tkinter', 'PySide6', 'PyQt5', 'PyQt6'}\n"
        "loaded = forbidden & set(sys.modules)\n"
        "assert not loaded, f'módulos proibidos carregados: {loaded}'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
