from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from market_analytics.tick_backfill import (
    BACKFILL_COLLECTOR_VERSION,
    ArtifactIdentityError,
    BackfillSessionRequest,
    build_raw_metadata,
    catalog_db_path,
    raw_partition_dir,
    session_window_utc,
    validate_artifact_identity,
)


def test_session_window_utc_converts_civil_day_in_sao_paulo_to_utc_bounds() -> None:
    window = session_window_utc(date(2026, 8, 28))

    # America/Sao_Paulo é UTC-03:00 (sem horário de verão desde 2019).
    assert window.start_utc == datetime(2026, 8, 28, 3, 0, tzinfo=UTC)
    assert window.end_utc == datetime(2026, 8, 29, 3, 0, tzinfo=UTC)
    assert (window.end_utc - window.start_utc).total_seconds() == 24 * 60 * 60


def test_session_window_utc_accepts_a_distinct_timezone_for_fot() -> None:
    """A mesma fundação deve ser neutra para uma prova na FOT (fora da B3)."""

    window = session_window_utc(date(2026, 8, 28), "America/New_York")

    assert window.start_utc.tzinfo is UTC
    assert window.end_utc.tzinfo is UTC
    assert (window.end_utc - window.start_utc).total_seconds() == 24 * 60 * 60


def _make_request(**overrides) -> BackfillSessionRequest:
    defaults: dict = dict(
        request_id="req-1",
        source_id="clear",
        logical_id="win",
        aliases=("WIN$",),
        tick_type="all",
        session_date=date(2026, 8, 28),
    )
    defaults.update(overrides)
    return BackfillSessionRequest(**defaults)


def test_request_round_trips_through_dict() -> None:
    request = _make_request(chunk_seconds=300, rebuild=True)

    restored = BackfillSessionRequest.from_dict(request.to_dict())

    assert restored == request


def test_request_rejects_uppercase_or_unsafe_source_id() -> None:
    with pytest.raises(ValueError):
        _make_request(source_id="Clear")
    with pytest.raises(ValueError):
        _make_request(source_id="clear/../etc")
    with pytest.raises(ValueError):
        _make_request(source_id="")


def test_request_rejects_datetime_as_session_date() -> None:
    with pytest.raises(ValueError):
        _make_request(session_date=datetime(2026, 8, 28, tzinfo=UTC))


def test_request_rejects_invalid_timezone() -> None:
    with pytest.raises(ValueError):
        _make_request(session_timezone="Not/AZone")


def test_request_rejects_chunk_seconds_outside_limits() -> None:
    with pytest.raises(ValueError):
        _make_request(chunk_seconds=901)
    with pytest.raises(ValueError):
        _make_request(chunk_seconds=59)


def test_fingerprint_changes_with_rebuild_flag() -> None:
    a = _make_request(rebuild=False)
    b = _make_request(rebuild=True)

    assert a.fingerprint() != b.fingerprint()


def test_fingerprint_is_stable_for_equivalent_requests() -> None:
    a = _make_request()
    b = _make_request()

    assert a.fingerprint() == b.fingerprint()


def test_raw_partition_dir_matches_documented_layout() -> None:
    path = raw_partition_dir(
        Path("D:/EPData/MarketHub"),
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
    )

    assert path == Path("D:/EPData/MarketHub/raw/clear/win/year=2026/month=08/session_date=2026-08-28")


def test_raw_partition_dir_never_collides_across_sources_or_logical_ids() -> None:
    common = dict(session_date=date(2026, 8, 28))
    win_clear = raw_partition_dir(Path("root"), source_id="clear", logical_id="win", **common)
    wdo_clear = raw_partition_dir(Path("root"), source_id="clear", logical_id="wdo", **common)
    win_fot = raw_partition_dir(Path("root"), source_id="fot", logical_id="win", **common)

    assert len({win_clear, wdo_clear, win_fot}) == 3


def test_catalog_db_path_is_fixed_and_outside_raw_tree() -> None:
    path = catalog_db_path(Path("D:/EPData/MarketHub"))

    assert path == Path("D:/EPData/MarketHub/catalog/collection.sqlite3")


def test_raw_metadata_never_includes_account_or_credentials() -> None:
    request = _make_request()

    metadata = build_raw_metadata(
        request=request,
        resolved_symbol="WIN$",
        collected_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
        attempt_id="attempt-1",
    )

    blob = " ".join(f"{k}={v}" for k, v in metadata.items()).lower()
    for forbidden in ("account", "login", "password", "senha", "conta", "credential"):
        assert forbidden not in blob
    assert metadata["source_id"] == "clear"
    assert metadata["resolved_symbol"] == "WIN$"
    assert metadata["schema_version"] == "1"


# --- Correção de auditoria item 1: desserialização estrita (sem truthiness) ---


def _payload(**overrides) -> dict:
    defaults: dict = dict(
        request_id="req-1",
        source_id="clear",
        logical_id="win",
        aliases=["WIN$"],
        tick_type="all",
        session_date="2026-08-28",
    )
    defaults.update(overrides)
    return defaults


def test_from_dict_rejects_string_false_as_rebuild() -> None:
    """Regressão principal da auditoria: `bool("false")` é `True` em Python;
    `from_dict` nunca pode coagir por truthiness."""

    with pytest.raises(ValueError, match="rebuild"):
        BackfillSessionRequest.from_dict(_payload(rebuild="false"))


def test_from_dict_rejects_string_true_as_rebuild_too() -> None:
    with pytest.raises(ValueError, match="rebuild"):
        BackfillSessionRequest.from_dict(_payload(rebuild="true"))


@pytest.mark.parametrize("bad_rebuild", ["false", "true", 0, 1, None, [], {}])
def test_from_dict_rejects_any_non_bool_rebuild(bad_rebuild) -> None:
    with pytest.raises(ValueError, match="rebuild"):
        BackfillSessionRequest.from_dict(_payload(rebuild=bad_rebuild))


def test_from_dict_accepts_real_bool_rebuild() -> None:
    request = BackfillSessionRequest.from_dict(_payload(rebuild=True))
    assert request.rebuild is True
    request = BackfillSessionRequest.from_dict(_payload(rebuild=False))
    assert request.rebuild is False


@pytest.mark.parametrize("bad_chunk_seconds", ["900", 900.0, True, None, "900s"])
def test_from_dict_rejects_non_int_chunk_seconds(bad_chunk_seconds) -> None:
    with pytest.raises(ValueError, match="chunk_seconds"):
        BackfillSessionRequest.from_dict(_payload(chunk_seconds=bad_chunk_seconds))


def test_from_dict_rejects_aliases_as_a_bare_string() -> None:
    """`aliases="WIN$"` iterado como string produziria um alias por
    caractere ('W', 'I', 'N', ...); deve ser recusado explicitamente."""

    with pytest.raises(ValueError, match="aliases"):
        BackfillSessionRequest.from_dict(_payload(aliases="WIN$"))


def test_from_dict_rejects_none_for_required_string_fields() -> None:
    for field_name in ("request_id", "source_id", "logical_id", "tick_type", "session_date"):
        with pytest.raises(ValueError):
            BackfillSessionRequest.from_dict(_payload(**{field_name: None}))


def test_from_dict_rejects_numeric_session_date() -> None:
    with pytest.raises(ValueError):
        BackfillSessionRequest.from_dict(_payload(session_date=20260828))


# --- Correção de auditoria item 2: slug de identidade/caminho ---


def test_slug_rejects_purely_numeric_source_id() -> None:
    """Um identificador só de dígitos poderia ser confundido com um número
    de conta; o slug precisa começar por letra."""

    with pytest.raises(ValueError):
        _make_request(source_id="123456")
    with pytest.raises(ValueError):
        _make_request(logical_id="99")


def test_slug_rejects_a_segment_starting_with_digit_even_if_mixed() -> None:
    with pytest.raises(ValueError):
        _make_request(source_id="1clear")


def test_slug_enforces_a_maximum_length() -> None:
    with pytest.raises(ValueError):
        _make_request(source_id="c" * 65)
    # 64 caracteres, começando por letra, continua válido.
    _make_request(source_id="c" * 64)


def test_raw_partition_dir_revalidates_segments_even_when_called_directly() -> None:
    """As funções públicas de caminho não confiam apenas no chamador."""

    with pytest.raises(ValueError):
        raw_partition_dir(Path("root"), source_id="123456", logical_id="win", session_date=date(2026, 8, 28))
    with pytest.raises(ValueError):
        raw_partition_dir(Path("root"), source_id="clear", logical_id="../etc", session_date=date(2026, 8, 28))


# --- Correção de auditoria item 3: dia civil com DST (23h/25h) ---


def test_session_window_covers_a_23_hour_spring_forward_day_in_new_york() -> None:
    """2026-03-08: relógios avançam de 2h para 3h em America/New_York."""

    window = session_window_utc(date(2026, 3, 8), "America/New_York")

    assert (window.end_utc - window.start_utc).total_seconds() == 23 * 3600


def test_session_window_covers_a_25_hour_fall_back_day_in_new_york() -> None:
    """2026-11-01: relógios recuam de 2h para 1h em America/New_York."""

    window = session_window_utc(date(2026, 11, 1), "America/New_York")

    assert (window.end_utc - window.start_utc).total_seconds() == 25 * 3600


@pytest.mark.parametrize(
    "session_date,tz_name",
    [
        (date(2026, 3, 8), "America/New_York"),  # 23h
        (date(2026, 8, 28), "America/Sao_Paulo"),  # 24h
        (date(2026, 11, 1), "America/New_York"),  # 25h
    ],
)
def test_session_chunks_are_contiguous_without_gap_or_overlap_across_dst(session_date, tz_name) -> None:
    window = session_window_utc(session_date, tz_name)
    chunks = window.chunks(900)

    assert chunks[0].start_utc == window.start_utc
    assert chunks[-1].end_utc == window.end_utc
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert previous.end_utc == current.start_utc  # sem gap nem overlap
    assert all((c.end_utc - c.start_utc).total_seconds() <= 900 for c in chunks)
    total = sum((c.end_utc - c.start_utc).total_seconds() for c in chunks)
    assert total == (window.end_utc - window.start_utc).total_seconds()


def test_backfill_request_accepts_a_25_hour_fot_session_end_to_end() -> None:
    """Uma sessão de backfill inteira (contrato -> chunks) não pode falhar
    só por causa de uma transição real de horário de verão."""

    request = _make_request(
        source_id="fot", logical_id="eurusd", session_date=date(2026, 11, 1), session_timezone="America/New_York"
    )

    chunks = request.chunks()

    assert sum((c.end_utc - c.start_utc).total_seconds() for c in chunks) == 25 * 3600


# --- Correção de auditoria (segunda entrega) item 6: SessionWindow.chunks e build_raw_metadata ---


@pytest.mark.parametrize("bad_chunk_seconds", [0, -1, -900, True, False, 59.0, 900.5, 59, 901])
def test_session_window_chunks_rejects_degenerate_or_out_of_range_values(bad_chunk_seconds) -> None:
    window = session_window_utc(date(2026, 8, 28))

    with pytest.raises(ValueError, match="chunk_seconds"):
        window.chunks(bad_chunk_seconds)


def test_session_window_chunks_zero_never_loops_forever() -> None:
    """Evidência da auditoria: chunks(0) precisa falhar imediatamente, não
    entrar num laço sem avanço."""

    window = session_window_utc(date(2026, 8, 28))

    with pytest.raises(ValueError):
        window.chunks(0)


def test_build_raw_metadata_rejects_naive_collected_at() -> None:
    request = _make_request()

    with pytest.raises(ValueError, match="collected_at"):
        build_raw_metadata(
            request=request, resolved_symbol="WIN$", collected_at=datetime(2026, 8, 28, 12), attempt_id="attempt-1"
        )


def test_build_raw_metadata_rejects_non_utc_collected_at() -> None:
    request = _make_request()
    non_utc = datetime(2026, 8, 28, 12, tzinfo=UTC).astimezone(ZoneInfo("America/Sao_Paulo"))

    with pytest.raises(ValueError, match="collected_at"):
        build_raw_metadata(request=request, resolved_symbol="WIN$", collected_at=non_utc, attempt_id="attempt-1")


def test_build_raw_metadata_rejects_empty_resolved_symbol() -> None:
    request = _make_request()

    with pytest.raises(ValueError, match="resolved_symbol"):
        build_raw_metadata(
            request=request,
            resolved_symbol="",
            collected_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
            attempt_id="attempt-1",
        )
    with pytest.raises(ValueError, match="resolved_symbol"):
        build_raw_metadata(
            request=request,
            resolved_symbol="   ",
            collected_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
            attempt_id="attempt-1",
        )


def test_build_raw_metadata_rejects_empty_attempt_id() -> None:
    request = _make_request()

    with pytest.raises(ValueError, match="attempt_id"):
        build_raw_metadata(
            request=request,
            resolved_symbol="WIN$",
            collected_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
            attempt_id="",
        )


# --- Correção de auditoria (segunda entrega) item 2: identidade semântica do artefato ---


def _valid_metadata(request: BackfillSessionRequest, *, resolved_symbol: str = "WIN$", attempt_id: str = "attempt-1") -> dict:
    return build_raw_metadata(
        request=request,
        resolved_symbol=resolved_symbol,
        collected_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
        attempt_id=attempt_id,
    )


def test_validate_artifact_identity_accepts_matching_metadata_and_returns_file_facts() -> None:
    request = _make_request()
    metadata = _valid_metadata(request, resolved_symbol="WIN$", attempt_id="attempt-42")

    identity = validate_artifact_identity(metadata, request=request)

    assert identity.resolved_symbol == "WIN$"
    assert identity.attempt_id == "attempt-42"
    assert identity.collector_version == BACKFILL_COLLECTOR_VERSION
    assert identity.collected_at_utc == datetime(2026, 8, 28, 12, tzinfo=UTC)


def test_validate_artifact_identity_rejects_wrong_source_at_the_same_path() -> None:
    """Reprodução da auditoria: um arquivo válido de outra fonte/ativo/data
    nunca deve ser adotado silenciosamente."""

    request = _make_request(source_id="clear", logical_id="win", session_date=date(2026, 8, 28))
    foreign_request = _make_request(
        source_id="fot", logical_id="eurusd", session_date=date(2020, 1, 1), session_timezone="America/New_York"
    )
    metadata = _valid_metadata(foreign_request, resolved_symbol="EURUSD")

    with pytest.raises(ArtifactIdentityError):
        validate_artifact_identity(metadata, request=request)


@pytest.mark.parametrize(
    "key,replacement",
    [
        ("source_id", "fot"),
        ("logical_id", "wdo"),
        ("session_date", "2020-01-01"),
        ("session_timezone", "America/New_York"),
        ("tick_type", "info"),
        ("schema", "something.else"),
        ("schema_version", "999"),
        ("requested_start_utc", "2020-01-01T00:00:00+00:00"),
        ("requested_end_utc", "2020-01-02T00:00:00+00:00"),
        ("collector_version", "unknown-future-or-foreign"),
        ("collected_at_utc", "not-a-datetime"),
    ],
)
def test_validate_artifact_identity_rejects_each_divergent_field(key, replacement) -> None:
    request = _make_request()
    metadata = dict(_valid_metadata(request))
    metadata[key] = replacement

    with pytest.raises(ArtifactIdentityError):
        validate_artifact_identity(metadata, request=request)


def test_validate_artifact_identity_rejects_a_naive_collected_at_in_the_file() -> None:
    """collected_at_utc precisa ser timezone-aware em UTC também no arquivo
    já gravado, não só na hora de escrever (item 6 da terceira auditoria)."""

    request = _make_request()
    metadata = dict(_valid_metadata(request))
    metadata["collected_at_utc"] = "2026-08-28T12:00:00"  # sem tzinfo

    with pytest.raises(ArtifactIdentityError, match="collected_at_utc"):
        validate_artifact_identity(metadata, request=request)


def test_validate_artifact_identity_rejects_unknown_collector_version() -> None:
    """Reprodução da auditoria: uma versão de coletor desconhecida não pode
    ser aceita silenciosamente — só a versão atual é compatível."""

    request = _make_request()
    metadata = dict(_valid_metadata(request))
    metadata["collector_version"] = "unknown-future-or-foreign"

    with pytest.raises(ArtifactIdentityError, match="collector_version"):
        validate_artifact_identity(metadata, request=request)


@pytest.mark.parametrize(
    "missing_key",
    [
        "schema",
        "schema_version",
        "source_id",
        "logical_id",
        "session_date",
        "collector_version",
        "resolved_symbol",
        "attempt_id",
        "collected_at_utc",
    ],
)
def test_validate_artifact_identity_rejects_missing_metadata(missing_key) -> None:
    request = _make_request()
    metadata = dict(_valid_metadata(request))
    del metadata[missing_key]

    with pytest.raises(ArtifactIdentityError):
        validate_artifact_identity(metadata, request=request)


def test_validate_artifact_identity_rejects_empty_string_metadata_value() -> None:
    request = _make_request()
    metadata = dict(_valid_metadata(request))
    metadata["resolved_symbol"] = ""

    with pytest.raises(ArtifactIdentityError):
        validate_artifact_identity(metadata, request=request)
