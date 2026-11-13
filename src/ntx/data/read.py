"""Typed catalog accessors.

Why this module exists: `ParquetDataCatalog.query()` takes `**kwargs`, so a
misspelled filter name is **silently ignored** and the query returns everything.
Calling `query(data_cls=Bar, bar_types=[...])` -- `bar_types` is not a real
parameter -- returned every bar for every symbol and every timeframe, with no
error. A strategy fed that would have produced interleaved 15m/4h bars from
multiple instruments and still emitted plausible-looking results.

That is failure pattern 5 (docs/13 §1): library parameter names are a contract,
not a suggestion. So all catalog reads go through these typed functions, and
`test_bar_type_filter_actually_filters` is the regression guard.
"""

from __future__ import annotations

from pathlib import Path

from nautilus_trader.model.data import Bar
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from ntx.data.catalog import bar_type_for

__all__ = ["available_bar_types", "open_catalog", "read_bars", "read_instrument"]


def open_catalog(data_dir: Path) -> ParquetDataCatalog:
    """Open the catalog at `data_dir/catalog`. Path must exist -- R21."""
    path = (Path(data_dir) / "catalog").resolve()
    if not path.exists():
        raise FileNotFoundError(f"no catalog at {path}; run `make data` first")
    return ParquetDataCatalog(str(path))


def available_bar_types(data_dir: Path) -> list[str]:
    """Bar-type names present on disk (one directory per bar type)."""
    bar_dir = (Path(data_dir) / "catalog" / "data" / "bar").resolve()
    if not bar_dir.exists():
        return []
    return sorted(p.name for p in bar_dir.iterdir() if p.is_dir())


def read_instrument(catalog: ParquetDataCatalog, instrument_id: str) -> Instrument:
    for inst in catalog.instruments():
        if str(inst.id) == instrument_id:
            return inst
    raise KeyError(f"{instrument_id} not in catalog")


def read_bars(
    catalog: ParquetDataCatalog,
    instrument_id: str,
    timeframe: str,
    *,
    start: str | None = None,
    end: str | None = None,
) -> list[Bar]:
    """Read exactly one instrument's bars at exactly one timeframe.

    Asserts the result is homogeneous: a mixed-bar_type result means the filter
    was ignored again, and silently wrong data is worse than an exception.
    """
    inst = read_instrument(catalog, instrument_id)
    bar_type = bar_type_for(inst, timeframe)  # type: ignore[arg-type]
    bars = catalog.query(data_cls=Bar, identifiers=[str(bar_type)], start=start, end=end)

    distinct = {str(b.bar_type) for b in bars}
    if len(distinct) > 1:
        raise RuntimeError(
            f"catalog returned {len(distinct)} bar types for a single-type query "
            f"({sorted(distinct)[:3]}...) -- the filter was ignored"
        )
    if bars and distinct != {str(bar_type)}:
        raise RuntimeError(f"asked for {bar_type}, received {distinct}")
    return bars
