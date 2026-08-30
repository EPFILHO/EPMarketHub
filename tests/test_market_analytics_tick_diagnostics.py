from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone

import pytest

from market_analytics.tick_diagnostics import (
    CANONICAL_TICK_TYPES,
    EMPTY_REASON_NO_TICKS,
    MAX_CHUNK_SECONDS,
    MAX_TOTAL_SECONDS,
    MAX_WINDOW_SECONDS,
    MAX_WINDOWS_PER_REQUEST,
    MIN_CHUNK_SECONDS,
    TickRecord,
    TickWindow,
    TickWindowAccumulator,
    TickWindowRequest,
    mt5_tick_type_attr,
    validate_tick_record,
)

UTC_START = datetime(2026, 3, 2, 13, 0, tzinfo=UTC)


# --- TickWindow ---------------------------------------------------------


def test_window_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        TickWindow(start_utc=datetime(2026, 3, 2, 13, 0), end_utc=UTC_START + timedelta(hours=1))


def test_window_rejects_non_utc_offset() -> None:
    other_tz = timezone(timedelta(hours=-3))
    with pytest.raises(ValueError, match="UTC"):
        TickWindow(
            start_utc=UTC_START.astimezone(other_tz),
            end_utc=UTC_START + timedelta(hours=1),
        )


def test_window_rejects_end_not_after_start() -> None:
    with pytest.raises(ValueError, match="maior"):
        TickWindow(start_utc=UTC_START, end_utc=UTC_START)


def test_window_rejects_duration_above_max() -> None:
    with pytest.raises(ValueError, match="máximo permitido"):
        TickWindow(
            start_utc=UTC_START, end_utc=UTC_START + timedelta(seconds=MAX_WINDOW_SECONDS + 1)
        )


def test_window_chunks_cover_range_contiguously_without_gap_or_overlap() -> None:
    window = TickWindow(start_utc=UTC_START, end_utc=UTC_START + timedelta(seconds=100))

    chunks = window.chunks(chunk_seconds=30)

    assert chunks[0].start_utc == window.start_utc
    assert chunks[-1].end_utc == window.end_utc
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert previous.end_utc == current.start_utc


# --- tick_type -----------------------------------------------------------


def test_mt5_tick_type_attr_maps_all_canonical_values() -> None:
    mapped = {t: mt5_tick_type_attr(t) for t in CANONICAL_TICK_TYPES}
    assert mapped == {
        "all": "COPY_TICKS_ALL",
        "info": "COPY_TICKS_INFO",
        "trade": "COPY_TICKS_TRADE",
    }


def test_mt5_tick_type_attr_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="inválido"):
        mt5_tick_type_attr("bogus")


# --- TickWindowRequest: limites rígidos exigidos pela auditoria ----------


def _window(offset_minutes: int = 0, duration_minutes: int = 30) -> TickWindow:
    start = UTC_START + timedelta(minutes=offset_minutes)
    return TickWindow(start_utc=start, end_utc=start + timedelta(minutes=duration_minutes))


def make_request(**overrides) -> TickWindowRequest:
    defaults: dict = dict(
        request_id="req-1",
        logical_id="win",
        aliases=("WIN$",),
        tick_type="all",
        windows=(_window(),),
        chunk_seconds=60,
    )
    defaults.update(overrides)
    return TickWindowRequest(**defaults)


def test_request_rejects_more_windows_than_allowed() -> None:
    windows = tuple(_window(offset_minutes=i * 60) for i in range(MAX_WINDOWS_PER_REQUEST + 1))
    with pytest.raises(ValueError, match="quantidade de janelas"):
        make_request(windows=windows)


def test_request_rejects_total_duration_above_max_even_within_window_count() -> None:
    long_window = TickWindow(
        start_utc=UTC_START, end_utc=UTC_START + timedelta(seconds=MAX_WINDOW_SECONDS)
    )
    windows = (long_window, long_window, long_window)
    assert len(windows) <= MAX_WINDOWS_PER_REQUEST
    with pytest.raises(ValueError, match="duração total"):
        make_request(windows=windows)


def test_request_accepts_total_duration_at_the_limit() -> None:
    half = MAX_TOTAL_SECONDS / 2
    windows = (
        TickWindow(start_utc=UTC_START, end_utc=UTC_START + timedelta(seconds=half)),
        TickWindow(
            start_utc=UTC_START + timedelta(days=1),
            end_utc=UTC_START + timedelta(days=1, seconds=half),
        ),
    )
    make_request(windows=windows)  # não deve levantar


@pytest.mark.parametrize("chunk_seconds", [0, 59, MAX_CHUNK_SECONDS + 1, -10])
def test_request_rejects_chunk_seconds_outside_bounds(chunk_seconds: int) -> None:
    with pytest.raises(ValueError, match="chunk_seconds"):
        make_request(chunk_seconds=chunk_seconds)


def test_request_rejects_chunk_seconds_above_900_regression() -> None:
    """COR-DEV-001 item 7: o teto de chunk_seconds foi reduzido para 900s."""

    assert MAX_CHUNK_SECONDS == 900
    with pytest.raises(ValueError, match="chunk_seconds"):
        make_request(chunk_seconds=901)
    make_request(chunk_seconds=900)  # no limite, não deve levantar


def test_request_accepts_chunk_seconds_bounds() -> None:
    make_request(chunk_seconds=MIN_CHUNK_SECONDS)
    make_request(chunk_seconds=MAX_CHUNK_SECONDS)


def test_request_rejects_unknown_tick_type() -> None:
    with pytest.raises(ValueError, match="tick_type"):
        make_request(tick_type="bogus")


def test_request_rejects_empty_request_id() -> None:
    with pytest.raises(ValueError, match="request_id"):
        make_request(request_id="  ")


def test_request_rejects_no_aliases() -> None:
    with pytest.raises(ValueError, match="aliases"):
        make_request(aliases=())


# --- fingerprint / round-trip ---------------------------------------------


def test_identical_requests_have_the_same_fingerprint() -> None:
    a = make_request()
    b = make_request()
    assert a.fingerprint() == b.fingerprint()


def test_different_windows_change_the_fingerprint() -> None:
    a = make_request()
    b = make_request(windows=(_window(offset_minutes=5),))
    assert a.fingerprint() != b.fingerprint()


def test_request_round_trips_through_dict() -> None:
    original = make_request()
    restored = TickWindowRequest.from_dict(original.to_dict())
    assert restored == original
    assert restored.fingerprint() == original.fingerprint()


# --- TickRecord / validate_tick_record ------------------------------------


def _record(time_msc: int, *, time: int | None = None, **overrides) -> TickRecord:
    defaults = dict(bid=0.0, ask=0.0, last=0.0, volume=0.0, volume_real=0.0, flags=0)
    defaults.update(overrides)
    resolved_time = time_msc // 1000 if time is None else time
    return TickRecord(time=resolved_time, time_msc=time_msc, **defaults)


def test_validate_tick_record_accepts_all_zero_values() -> None:
    validate_tick_record(_record(0))  # não deve levantar


@pytest.mark.parametrize("field_name", ["bid", "ask", "last", "volume", "volume_real"])
def test_validate_tick_record_rejects_nan_and_infinite_values(field_name: str) -> None:
    for bad_value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError, match="não finito"):
            validate_tick_record(_record(1000, **{field_name: bad_value}))


def test_validate_tick_record_rejects_incoherent_time_and_time_msc() -> None:
    with pytest.raises(ValueError, match="incoerentes"):
        validate_tick_record(
            TickRecord(
                time=1, time_msc=5000, bid=0, ask=0, last=0, volume=0, volume_real=0, flags=0
            )
        )


def test_validate_tick_record_rejects_negative_flags() -> None:
    with pytest.raises(ValueError, match="flags"):
        validate_tick_record(_record(1000, flags=-1))


def test_validate_tick_record_rejects_negative_time_msc() -> None:
    with pytest.raises(ValueError, match="time_msc"):
        validate_tick_record(_record(-1))


# --- TickWindowAccumulator: consumo incremental ---------------------------


def _summary_kwargs(**overrides) -> dict:
    defaults = dict(
        window=_window(),
        request_id="req-1",
        pid=1,
        source_id="s",
        logical_id="win",
        resolved_symbol="WIN$",
        tick_type="all",
    )
    defaults.update(overrides)
    return defaults


def test_empty_window_reports_canonical_reason_without_inferring_cause() -> None:
    accumulator = TickWindowAccumulator()

    summary = accumulator.finalize(**_summary_kwargs())

    assert summary.total_count == 0
    assert summary.empty_reason == EMPTY_REASON_NO_TICKS
    assert summary.first_time_utc is None
    assert summary.first_time is None


def test_consume_rejects_invalid_record_before_counting_it() -> None:
    accumulator = TickWindowAccumulator()

    with pytest.raises(ValueError):
        accumulator.consume(_record(1000, bid=math.nan))

    assert accumulator.total_count == 0


def test_non_zero_counts_are_tracked_per_field() -> None:
    accumulator = TickWindowAccumulator()
    accumulator.consume(_record(1000, bid=5.0))
    accumulator.consume(_record(2000, ask=6.0))
    accumulator.consume(_record(3000))

    summary = accumulator.finalize(**_summary_kwargs())

    assert summary.total_count == 3
    assert summary.non_zero_counts["bid"] == {"non_zero": 1, "total": 3}
    assert summary.non_zero_counts["ask"] == {"non_zero": 1, "total": 3}
    assert summary.non_zero_counts["last"] == {"non_zero": 0, "total": 3}


def test_flags_histogram_decodes_bits() -> None:
    accumulator = TickWindowAccumulator()
    accumulator.consume(_record(1000, flags=2 | 4))  # bid + ask
    accumulator.consume(_record(2000, flags=32))  # buy

    summary = accumulator.finalize(**_summary_kwargs())

    assert summary.flags_histogram["bid"] == 1
    assert summary.flags_histogram["ask"] == 1
    assert summary.flags_histogram["buy"] == 1
    assert summary.flags_histogram["sell"] == 0


def test_out_of_order_and_duplicate_detection_works_across_separate_chunk_calls() -> None:
    """O acumulador não recebe o array inteiro de uma vez: dois chunks
    separados devem detectar desordem/duplicata/empate na fronteira mesmo
    assim, sem reprocessar nada do primeiro chunk."""

    accumulator = TickWindowAccumulator()

    # Chunk 1.
    accumulator.consume(_record(1000, bid=1.0))
    accumulator.consume(_record(2000, bid=1.5))

    # Chunk 2, processado numa chamada separada: duplicata exata do último
    # tick do chunk 1, um empate de time_msc com dado diferente, e um tick
    # fora de ordem.
    accumulator.consume(_record(2000, bid=1.5))  # duplicata exata
    accumulator.consume(_record(2000, bid=1.6))  # empate, dado diferente
    accumulator.consume(_record(1500, bid=1.7))  # fora de ordem

    summary = accumulator.finalize(**_summary_kwargs())

    assert summary.total_count == 5
    assert summary.exact_duplicate_count == 1
    assert summary.time_msc_tie_count == 2
    assert summary.out_of_order_count == 1


def test_out_of_order_arrival_never_produces_negative_duration_or_gaps() -> None:
    """COR-DEV-001 item 2 (regressão): ticks chegando em 3000ms e depois em
    1000ms (relativos ao início da janela) devem contar 1 desordem, mas a
    duração factual usa min/max de time_msc — nunca a ordem de chegada — e
    por isso nunca fica negativa."""

    window = _window()
    base_ms = int(window.start_utc.timestamp() * 1000)
    accumulator = TickWindowAccumulator()
    accumulator.consume(_record(base_ms + 3000))
    accumulator.consume(_record(base_ms + 1000))

    summary = accumulator.finalize(**_summary_kwargs(window=window))

    assert summary.out_of_order_count == 1
    assert summary.first_time_msc == base_ms + 1000
    assert summary.last_time_msc == base_ms + 3000
    assert summary.observed_duration_seconds == pytest.approx(2.0)
    assert summary.observed_duration_seconds >= 0
    assert summary.leading_gap_seconds >= 0
    assert summary.trailing_gap_seconds >= 0


def test_largest_gaps_are_bounded_and_sorted_descending() -> None:
    accumulator = TickWindowAccumulator(top_gaps=2)
    times = [0, 1000, 3000, 4000, 20000]  # gaps: 1s, 2s, 1s, 16s
    for t in times:
        accumulator.consume(_record(t))

    summary = accumulator.finalize(**_summary_kwargs())

    assert summary.largest_gaps_seconds == [16.0, 2.0]


def test_summary_reports_factual_metrics_not_a_completeness_proof() -> None:
    window = _window(duration_minutes=10)
    accumulator = TickWindowAccumulator()
    first_ms = int(window.start_utc.timestamp() * 1000) + 5_000
    last_ms = int(window.start_utc.timestamp() * 1000) + 400_000
    accumulator.consume(_record(first_ms))
    accumulator.consume(_record(last_ms))

    summary = accumulator.finalize(**_summary_kwargs(window=window))

    assert summary.observed_duration_seconds == pytest.approx((last_ms - first_ms) / 1000.0)
    assert summary.leading_gap_seconds == pytest.approx(5.0)
    assert not hasattr(summary, "coverage_ratio")
    assert "não" in summary.coverage_disclaimer.lower()


def test_summary_preserves_raw_time_and_time_msc_alongside_utc() -> None:
    accumulator = TickWindowAccumulator()
    accumulator.consume(_record(1_700_000_000_500, time=1_700_000_000))

    summary = accumulator.finalize(**_summary_kwargs())

    assert summary.first_time == 1_700_000_000
    assert summary.first_time_msc == 1_700_000_000_500
    assert summary.last_time == 1_700_000_000
    assert summary.last_time_msc == 1_700_000_000_500
    assert summary.first_time_utc is not None
    payload = summary.to_dict()
    assert payload["first_time"] == 1_700_000_000
    assert payload["first_time_msc"] == 1_700_000_000_500


def test_summary_uses_single_pid_field_not_worker_pid() -> None:
    summary = TickWindowAccumulator().finalize(**_summary_kwargs(pid=4321))

    payload = summary.to_dict()
    assert payload["pid"] == 4321
    assert "worker_pid" not in payload
