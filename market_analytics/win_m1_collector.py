"""Coletor offline/read-only de barras M1 do WIN$ via `copy_rates_range` (DEV-007).

Produtor da entrega A do Fusion Quant DEV-009C.1: solicita cada mês
separadamente a um terminal MT5 explícito, valida a resposta e converte em
`Bar` (`market_analytics.bars`) prontas para o pipeline de features
existente. Este módulo não integra GUI, bridge, worker, protocolo, polling
ou kernel do aplicativo — é lido/chamado somente pela CLI em
`tools/collect_win_m1_history.py` e pelo orquestrador
`market_analytics.win_m1_features`.

A dependência da biblioteca `MetaTrader5` fica inteiramente atrás de
`RatesProvider` (um `Protocol` estrutural — qualquer objeto com o método
certo serve) e do adaptador real `Mt5RatesProvider`, que só importa
`MetaTrader5` dentro dos próprios métodos. Importar este módulo nunca exige
o pacote `MetaTrader5` nem toca um terminal; só instanciar e usar
`Mt5RatesProvider` faz isso — e nenhuma parte desta entrega instancia ou
chama esse adaptador.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from .bars import Bar, VolumeQuality
from .win_m1_inventory import RawM1Row

# Janela semiaberta congelada pelo DEV-007: 2026-01-01 00:00:00 (inclusive)
# até 2026-08-01 00:00:00 (exclusive), UTC — sete meses, jan..jul/2026.
FROZEN_WINDOW_START = date(2026, 1, 1)
FROZEN_WINDOW_END_EXCLUSIVE = date(2026, 8, 1)

# Campos exigidos, nesta ordem, na tupla que `RatesProvider.copy_rates_range`
# devolve por barra — os mesmos oito campos canônicos do inventário.
RAW_ROW_FIELD_COUNT = 8


class MonthRejectedError(Exception):
    """Um mês inteiro foi recusado: retorno vazio, timestamp fora de ordem/mês,
    OHLC inválido ou linha malformada. Carrega `month`/`reason` para que o
    chamador registre um alerta preciso e interrompa a coleta (DEV-007 exige
    tudo-ou-nada por mês; não há reconciliação parcial de mês)."""

    def __init__(self, month: str, reason: str) -> None:
        super().__init__(f"{month}: {reason}")
        self.month = month
        self.reason = reason


class RatesProvider(Protocol):
    """Interface mínima e injetável em torno de `copy_rates_range`.

    Devolve uma sequência de tuplas de 8 elementos na ordem canônica
    ``(time, open, high, low, close, tick_volume, spread, real_volume)`` —
    ou `None`/vazio quando o terminal não tem dados para a janela. Nunca
    lança por conta própria em dado inválido; a validação é sempre feita por
    `fetch_and_validate_month`, para que o motivo da rejeição fique
    centralizado e testável em um só lugar.
    """

    def copy_rates_range(
        self, *, symbol: str, date_from: datetime, date_to: datetime
    ) -> Sequence[tuple[Any, ...]] | None: ...


class Mt5RatesProvider:
    """Adaptador real contra a biblioteca `MetaTrader5` (reservado para execução futura).

    Importa `MetaTrader5` somente dentro de `__enter__`/`copy_rates_range`
    (nunca no nível do módulo), então `import market_analytics.win_m1_collector`
    não exige o pacote nem toca um terminal. Esta entrega (Claude/DEV-007) não
    instancia nem chama esta classe em nenhum teste ou execução — ela existe
    só para que o comando de execução futura real (ver
    `tools/collect_win_m1_history.py`) tenha um adaptador funcional pronto,
    sem reabrir esse trabalho depois.

    `__enter__` recusa claramente (e desliga o terminal antes de propagar o
    erro) em qualquer uma destas três falhas, na ordem: `initialize()`
    retorna falso; `terminal_info()` é `None` ou `connected` é falso (MT5
    abriu mas não está conectado à corretora); `symbol_select(symbol, True)`
    retorna falso (símbolo indisponível/não selecionável no Market Watch).
    Nenhuma consulta de barras é feita antes dessas três confirmações —
    auditoria Codex da 1ª entrega do DEV-007.
    """

    def __init__(self, terminal_path: str, *, symbol: str = "WIN$") -> None:
        if not isinstance(terminal_path, str) or not terminal_path.strip():
            raise ValueError("terminal_path não pode ser vazio")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol não pode ser vazio")
        self._terminal_path = terminal_path
        self._symbol = symbol
        self._mt5: Any = None

    def __enter__(self) -> Mt5RatesProvider:
        import MetaTrader5 as mt5  # import tardio deliberado — ver docstring da classe

        if not mt5.initialize(path=self._terminal_path):
            error = mt5.last_error()
            raise RuntimeError(f"falha ao inicializar o terminal MT5 em {self._terminal_path}: {error}")

        info = mt5.terminal_info()
        if info is None or not getattr(info, "connected", False):
            mt5.shutdown()
            raise RuntimeError(
                f"terminal MT5 inicializado em {self._terminal_path} mas não conectado à corretora "
                f"(terminal_info()={info!r})"
            )

        if not mt5.symbol_select(self._symbol, True):
            error = mt5.last_error()
            mt5.shutdown()
            raise RuntimeError(f"falha ao selecionar o símbolo {self._symbol!r} no terminal: {error}")

        self._mt5 = mt5
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()
            self._mt5 = None

    def copy_rates_range(
        self, *, symbol: str, date_from: datetime, date_to: datetime
    ) -> list[tuple[Any, ...]] | None:
        if self._mt5 is None:
            raise RuntimeError("Mt5RatesProvider usado fora do bloco 'with' (initialize não foi chamado)")
        rates = self._mt5.copy_rates_range(symbol, self._mt5.TIMEFRAME_M1, date_from, date_to)
        if rates is None or len(rates) == 0:
            return None
        return [
            (
                int(row["time"]),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                int(row["tick_volume"]),
                int(row["spread"]),
                int(row["real_volume"]),
            )
            for row in rates
        ]


def month_windows(start: date, end_exclusive: date) -> list[tuple[str, datetime, datetime, datetime]]:
    """Gera as janelas mensais semiabertas `[YYYY-MM-01, próximo_mês-01)` em UTC.

    `start` e `end_exclusive` devem cair no dia 1 de um mês. Retorna, por
    mês, uma tupla `(rótulo "YYYY-MM", início_utc_inclusivo,
    fim_utc_exclusivo, fim_utc_de_solicitação)`:

    - `início`/`fim_exclusivo` definem a janela **semiaberta** usada para
      validar semanticamente cada barra devolvida (`fetch_and_validate_month`
      recusa qualquer timestamp fora de `[início, fim_exclusivo)`);
    - `fim_de_solicitação` é `fim_exclusivo` menos 1 segundo — ou seja,
      `23:59:59` do último dia do mês — porque `copy_rates_range` do MT5
      trata o argumento `date_to` como **inclusivo**: solicitar
      `00:00:00` do mês seguinte arriscaria devolver também a primeira
      barra do mês seguinte. O inventário congelado pelo Fusion Quant foi
      gerado com esse mesmo fim inclusivo por mês (DEV-007, correção da
      auditoria Codex); a validação semântica continua semiaberta.
    """

    if start.day != 1:
        raise ValueError(f"start deve ser o dia 1 de um mês (recebido: {start.isoformat()})")
    if end_exclusive.day != 1:
        raise ValueError(f"end_exclusive deve ser o dia 1 de um mês (recebido: {end_exclusive.isoformat()})")
    if end_exclusive <= start:
        raise ValueError(f"end_exclusive ({end_exclusive}) deve ser posterior a start ({start})")

    windows: list[tuple[str, datetime, datetime, datetime]] = []
    cursor = start
    while cursor < end_exclusive:
        year, month = cursor.year, cursor.month
        next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        label = f"{year:04d}-{month:02d}"
        window_start = datetime(cursor.year, cursor.month, 1, tzinfo=UTC)
        window_end = datetime(next_month.year, next_month.month, 1, tzinfo=UTC)
        request_end = window_end - timedelta(seconds=1)
        windows.append((label, window_start, window_end, request_end))
        cursor = next_month
    return windows


def fetch_and_validate_month(
    provider: RatesProvider,
    *,
    symbol: str,
    month: str,
    window_start: datetime,
    window_end: datetime,
    request_end: datetime,
) -> list[RawM1Row]:
    """Solicita um único mês e aplica todas as recusas exigidas pelo DEV-007.

    `request_end` (inclusivo — `23:59:59` do último dia do mês, ver
    `month_windows`) é o que é efetivamente enviado ao provider como
    `date_to`, nunca `window_end` (que é exclusivo e serve só para a
    validação semântica abaixo). Isso reflete o comportamento real de
    `copy_rates_range` do MT5, cujo `date_to` é inclusivo.

    Ordem das checagens (todas levantam `MonthRejectedError` com o motivo
    exato): retorno vazio inesperado -> linha malformada/OHLC não finito (ao
    construir `RawM1Row`) -> ordena por `time` -> timestamp duplicado/não
    crescente -> timestamp fora do mês (`[window_start, window_end)`,
    semiaberto). A ordenação acontece antes da checagem de
    duplicidade/crescimento (conforme o texto do DEV-007: "ordenar por
    timestamp e recusar... duplicado ou não crescente"), então uma resposta
    fora de ordem só é rejeitada se produzir uma colisão de timestamp após
    ordenada — uma resposta MT5 genuinamente fora de ordem sem colisão seria
    silenciosamente corrigida pela ordenação, o que é aceitável porque o
    critério de aceite real (contagem + fingerprint contra o inventário)
    ainda audita o conteúdo exato do conjunto.
    """

    raw = provider.copy_rates_range(symbol=symbol, date_from=window_start, date_to=request_end)
    if not raw:
        raise MonthRejectedError(month, "retorno vazio inesperado do provider")

    rows: list[RawM1Row] = []
    for index, item in enumerate(raw):
        if not isinstance(item, tuple | list) or len(item) != RAW_ROW_FIELD_COUNT:
            raise MonthRejectedError(
                month, f"linha #{index} não tem os {RAW_ROW_FIELD_COUNT} campos canônicos: {item!r}"
            )
        try:
            row = RawM1Row(*item)
        except (TypeError, ValueError) as exc:
            raise MonthRejectedError(month, f"linha #{index} inválida: {exc}") from exc
        rows.append(row)

    rows.sort(key=lambda item: item.time)

    previous_time: int | None = None
    for row in rows:
        if previous_time is not None and row.time <= previous_time:
            raise MonthRejectedError(
                month, f"timestamp duplicado ou não crescente após ordenar: {previous_time} -> {row.time}"
            )
        previous_time = row.time

        timestamp = datetime.fromtimestamp(row.time, tz=UTC)
        if not (window_start <= timestamp < window_end):
            raise MonthRejectedError(
                month,
                f"timestamp fora do mês {month}: {timestamp.isoformat()} "
                f"(janela [{window_start.isoformat()}, {window_end.isoformat()}))",
            )

    return rows


def volume_for_row(row: RawM1Row) -> tuple[float, VolumeQuality]:
    """Política de qualidade de volume do DEV-007 (item B6), aplicada por barra.

    `real_volume` vira qualidade ``"exchange"`` somente quando for finito e
    não negativo (o `int` de `RawM1Row` já garante "finito"; só a checagem
    de sinal resta aqui). Caso contrário, cai para ``"tick_proxy"`` usando
    `tick_volume`. `tick_volume` do MT5 nunca é ausente para uma barra M1
    real, então esta função nunca produz `volume_quality == "missing"`.
    """

    if row.real_volume >= 0:
        return float(row.real_volume), "exchange"
    return float(row.tick_volume), "tick_proxy"


def raw_row_to_bar(row: RawM1Row, *, source_id: str, symbol: str) -> Bar:
    """Converte uma `RawM1Row` já validada numa `Bar` M1 fechada.

    Delega toda a validação de coerência OHLC (`high>=low`,
    `high>=max(open,close)`, `low<=min(open,close)`) para `Bar.__post_init__`
    — não duplica essa regra aqui. O chamador (`collect_validated_history`)
    reembala qualquer `ValueError` daqui como `MonthRejectedError`.
    """

    volume, quality = volume_for_row(row)
    return Bar(
        source_id=source_id,
        symbol=symbol,
        timeframe="M1",
        timestamp=datetime.fromtimestamp(row.time, tz=UTC),
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=volume,
        volume_quality=quality,
    )


def build_month_bars(month: str, rows: Sequence[RawM1Row], *, source_id: str, symbol: str) -> list[Bar]:
    """Converte um mês inteiro de `RawM1Row` já validadas em `Bar` M1.

    Uma barra OHLC incoerente (rejeitada por `Bar.__post_init__`) derruba o
    mês inteiro com `MonthRejectedError`, na mesma disciplina tudo-ou-nada
    das demais checagens deste módulo.
    """

    bars: list[Bar] = []
    for row in rows:
        try:
            bars.append(raw_row_to_bar(row, source_id=source_id, symbol=symbol))
        except ValueError as exc:
            raise MonthRejectedError(month, f"OHLC inválido na barra {row.time}: {exc}") from exc
    return bars
