"""
title: OWID Data Tools
author: Marco
version: 2.2.0
license: MIT
description: Search and fetch Our World in Data datasets, render results as
             interactive Plotly charts or raw tables. Dark-mode HTML with
             auto-resize iframes, fullscreen support, and ASCII fallback.
required_open_webui_version: 0.4.0

  ## Search & Data Access Workflow
  1. search_owid(query) -> returns slugs and brief descriptions.
  2. chart_owid_data(slug, ...) OR compare_owid_countries(...) OR get_owid_data(slug, ...) -> fetches data.
  3. Note: The catalog contains curated 'chart' objects. Use the 'slug' field from search results exactly.

  ## Catalog Coverage (Data Domains)
  - OWID catalog tracks 100+ topics including: demographics, economic development, education, energy, food security, health, inequality, poverty, sustainable development, war, and environment.
  - Data is organized as indicators (variables) within datasets/tables with rich metadata support.

  ## Country/Entity Handling
  - Use 'search_owid' to see the 'available_entities' list before charting.
  - Match names exactly (e.g., 'United States' not 'USA'). Aliases are handled internally for common names.
  - If an entity is not in 'available_entities', try another from the list provided.

  ## get_owid_data metadata discovery
  - When you first call get_owid_data(slug), if it lists many columns, pick the ones you need based on the column list returned, then call it again with the `columns` parameter.
"""
from __future__ import annotations

import html
import json
from typing import Any, List, Optional

import pandas as pd
from pydantic import BaseModel, Field  # Field used only in Valves

import difflib
import functools

try:
    from owid.catalog import search, fetch
except ImportError:
    search, fetch = None, None

try:
    from fastapi.responses import HTMLResponse
except Exception:
    HTMLResponse = None


# ── Colour palette (dark-mode, distinct) ─────────────────────────────────────
_SERIES_COLORS = [
    "#60a5fa", "#f472b6", "#34d399", "#fb923c",
    "#a78bfa", "#facc15", "#22d3ee", "#f87171",
]

_PLOTLY_CDN = "2.27.0"
_BG    = "#0b0f14"
_PANEL = "#111827"
_TEXT  = "#e5e7eb"
_MUTED = "#94a3b8"
_BORDER = "#374151"
_GRID  = "#1f2937"


# ── Helpers ───────────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=16)
def _cached_fetch_df(slug: str) -> pd.DataFrame:
    """Fetch from OWID catalog and cache the resulting DataFrame."""
    if fetch is None:
        raise RuntimeError("owid-catalog not installed.")
    table = fetch(slug)
    return pd.DataFrame(table).reset_index()

def _detect_cols(df: pd.DataFrame):
    """Return (country_col, year_col) by fuzzy name matching."""
    lc = {str(c).lower(): c for c in df.columns}
    country_col = next(
        (v for k, v in lc.items() if "entit" in k or "country" in k), None
    )
    year_col = next(
        (v for k, v in lc.items() if "year" in k or "date" in k or "day" in k), None
    )
    return country_col, year_col


def _detect_value_col(df: pd.DataFrame, structural: set, override: Optional[str]):
    """Return the first numeric column that isn't structural."""
    if override and override in df.columns:
        return override
    return next(
        (
            c for c in df.columns
            if str(c).lower() not in structural
            and pd.api.types.is_numeric_dtype(df[c])
        ),
        None,
    )


def _clean_series(
    df: pd.DataFrame,
    country_col,
    year_col: str,
    val_col: str,
    country: Optional[str],
    year_start: Optional[Any],
    year_end: Optional[Any],
) -> pd.DataFrame:
    """
    Filter, cast year to int, deduplicate (entity x year), sort.
    NaN values are dropped — never filled with 0.
    Returns a clean two-column frame [year_col, val_col].
    """
    # Country filter
    if country and country_col:
        available = df[country_col].dropna().unique().tolist()
        matched_country = _fuzzy_match_country(country, available)
        df = df[df[country_col].astype(str) == matched_country]

    df = df.copy()

    is_date = df[year_col].dtype.kind == 'M' or df[year_col].astype(str).str.match(r'^\d{4}-\d{2}-\d{2}').any()
    if is_date:
        df[year_col] = pd.to_datetime(df[year_col], errors="coerce")
    else:
        df[year_col] = pd.to_numeric(df[year_col], errors="coerce")
    df = df.dropna(subset=[year_col])
    if not is_date:
        df[year_col] = df[year_col].astype(int)

    # Year/Date range filter
    if year_start is not None:
        start_val = pd.to_datetime(year_start) if is_date else float(year_start)
        df = df[df[year_col] >= start_val]
    if year_end is not None:
        end_val = pd.to_datetime(year_end) if is_date else float(year_end)
        df = df[df[year_col] <= end_val]

    # Keep only the two columns we need; drop NaN values (never fill with 0)
    df = df[[year_col, val_col]].copy()
    df[val_col] = pd.to_numeric(df[val_col], errors="coerce")
    df = df.dropna(subset=[val_col])

    # Deduplicate: multiple source rows per year -> take mean
    df = df.groupby(year_col, as_index=False)[val_col].mean()

    return df.sort_values(year_col).reset_index(drop=True)


def _country_sample(df: pd.DataFrame, country_col) -> str:
    if country_col is None:
        return ""
    names = df[country_col].dropna().unique()[:14].tolist()
    return ", ".join(str(n) for n in names)

def _fuzzy_match_country(target: str, available: list) -> str:
    """Return exact match or closest fuzzy match for a country name."""
    if not target or not available:
        return target
    target_lower = target.strip().lower()
    # Standard abbreviations/aliases
    aliases = {
        "usa": "United States",
        "uk": "United Kingdom",
        "czech republic": "Czechia",
        "south korea": "South Korea",
        "north korea": "North Korea",
        "uae": "United Arab Emirates",
        "russia": "Russia",
        "vietnam": "Vietnam"
    }
    if target_lower in aliases:
        target_lower = aliases[target_lower].lower()
        
    for c in available:
        if str(c).strip().lower() == target_lower:
            return str(c)
    str_available = [str(c) for c in available]
    matches = difflib.get_close_matches(target, str_available, n=1, cutoff=0.5)
    return matches[0] if matches else target


def _build_html(title: str, traces: list, x_label: str, y_label: str, height: int = 460) -> str:
    """Render a self-contained dark-mode Plotly HTML page."""
    layout = {
        "title": {
            "text": title,
            "font": {"size": 15, "color": _TEXT},
            "x": 0.04,
        },
        "paper_bgcolor": _PANEL,
        "plot_bgcolor":  _BG,
        "font":   {"color": _TEXT, "family": "system-ui, -apple-system, sans-serif"},
        "margin": {"l": 72, "r": 24, "t": 52, "b": 52},
        "legend": {
            "bgcolor": "rgba(0,0,0,0)",
            "bordercolor": _BORDER,
            "borderwidth": 1,
            "font": {"color": _TEXT, "size": 12},
        },
        "xaxis": {
            "gridcolor": _GRID,
            "linecolor": _BORDER,
            "tickcolor": _BORDER,
            "tickformat": "d",        # integers only, no "2,020.5"
            "title": {"text": x_label, "font": {"size": 12, "color": _MUTED}},
            "tickfont": {"color": _MUTED},
        },
        "yaxis": {
            "gridcolor": _GRID,
            "linecolor": _BORDER,
            "tickcolor": _BORDER,
            "title": {"text": y_label, "font": {"size": 12, "color": _MUTED}},
            "tickfont": {"color": _MUTED},
            "zeroline": False,
            "tickformat": "~s",       # e.g. 10B, 500M, 2k
        },
        "hovermode": "x unified",
        "hoverlabel": {
            "bgcolor": _PANEL,
            "bordercolor": _BORDER,
            "font": {"color": _TEXT, "size": 12},
        },
    }

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{html.escape(title)}</title>
<style>
*,*::before,*::after{{box-sizing:border-box}}
html,body{{
  margin:0;padding:0;width:100%;
  background:{_BG};color:{_TEXT};
  font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  font-size:14px;overflow:visible;
}}
#chart{{width:100%;height:{height}px;padding:12px 12px 4px}}
#fs-btn{{
  position:fixed;top:8px;right:8px;z-index:9999;
  background:rgba(17,24,39,0.9);color:{_TEXT};
  border:1px solid {_BORDER};border-radius:6px;
  padding:3px 10px;cursor:pointer;font-size:11px;
  opacity:0.4;transition:opacity 0.2s;user-select:none;
}}
#fs-btn:hover{{opacity:1}}
</style>
</head><body>
<button id="fs-btn" onclick="toggleFS()">⛶ Fullscreen</button>
<div id="chart"></div>
<script src="https://cdn.plot.ly/plotly-{_PLOTLY_CDN}.min.js"></script>
<script>
(function(){{
  var traces = {json.dumps(traces)};
  var layout = {json.dumps(layout)};
  var config = {{
    responsive: true,
    displayModeBar: true,
    modeBarButtonsToRemove: ["select2d","lasso2d","autoScale2d","toggleSpikelines"],
    toImageButtonOptions: {{format:"png", scale:2, filename:{json.dumps(title)}}},
    displaylogo: false,
  }};
  Plotly.newPlot("chart", traces, layout, config);

  function toggleFS(){{
    if(!document.fullscreenElement)
      document.documentElement.requestFullscreen?.();
    else
      document.exitFullscreen?.();
  }}
  window.toggleFS = toggleFS;
  document.addEventListener("fullscreenchange", function(){{
    document.getElementById("fs-btn").textContent =
      document.fullscreenElement ? "✕ Exit" : "⛶ Fullscreen";
  }});

  function syncHeight(){{
    var h = document.documentElement.scrollHeight;
    try{{ if(window.frameElement) window.frameElement.style.height = h + "px"; }}catch(e){{}}
    try{{ window.parent.postMessage({{type:"iframe-resize", height:h}}, "*"); }}catch(e){{}}
  }}
  window.addEventListener("load", syncHeight);
  if(typeof ResizeObserver !== "undefined")
    new ResizeObserver(syncHeight).observe(document.body);
  [300, 900, 2000].forEach(function(t){{ setTimeout(syncHeight, t); }});
}})();
</script>
</body></html>"""


def _ascii_fallback(traces: list, title: str) -> str:
    h = 10
    lines = [f"### {title}", ""]
    for t in traces:
        y    = [v for v in t.get("y", []) if v is not None]
        x    = t.get("x", [])
        name = t.get("name", "")
        if not y:
            continue
        max_y = max(y)
        min_y = min(y)
        rng   = max_y - min_y or 1.0
        grid  = [[" "] * (len(y) * 2) for _ in range(h)]
        for i, v in enumerate(y):
            r = int((max_y - v) / rng * (h - 1))
            r = max(0, min(h - 1, r))
            grid[r][i * 2] = "●"
        if name:
            lines.append(f"**{name}**")
        lines.append("```text")
        for i, row in enumerate(grid):
            thr = max_y - (rng * i / (h - 1)) if h > 1 else max_y
            lines.append(f"{thr:9.2f} |{''.join(row)}")
        lines.append("         +" + "--" * len(y))
        tick_row = "           " + "".join(f"{str(x[i])[:4]:4} " for i in range(min(len(x), 12)))
        lines.append(tick_row)
        lines.append("```\n")
    return "\n".join(lines)


def _make_trace(x: list, y: list, name: str, color: str, chart_type: str = "line") -> dict:
    """
    Build a Plotly trace. y values are raw floats — never filled with 0.
    connectgaps=False means missing data shows as a gap, not a spike to zero.
    """
    ct = chart_type.lower().strip()
    if ct == "bar":
        return {
            "type":   "bar",
            "name":   name,
            "x":      x,
            "y":      y,
            "marker": {"color": color},
        }
    return {
        "type":        "scatter",
        "mode":        "lines+markers" if ct == "line" else "markers",
        "name":        name,
        "x":           x,
        "y":           y,
        "connectgaps": False,
        "line":        {"color": color, "width": 2},
        "marker":      {"size": 4, "color": color},
    }


# ─────────────────────────────────────────────────────────────────────────────

class Tools:
    """
    OWID Data Tools — search, fetch, chart, and compare Our World in Data.

    Workflow:
      1. search_owid   → discover slugs + valid country names
      2. chart_owid_data or compare_owid_countries → visualise
         get_owid_data → raw table (only when numbers are explicitly needed)
    """

    def __init__(self) -> None:
        self.valves = self.Valves()

    class Valves(BaseModel):
        allow_external_cdn: bool = Field(
            True,
            description="Load Plotly from CDN. If false, all charts fall back to ASCII.",
        )
        chart_height_px: int = Field(460, description="Chart height in pixels.")
        max_table_rows: int  = Field(20,  description="Max rows from get_owid_data.")
        max_search_results: int = Field(5, description="Max results from search_owid.")

    def _can_embed(self) -> bool:
        return self.valves.allow_external_cdn and HTMLResponse is not None

    def _render(self, title: str, traces: list, x_label: str, y_label: str) -> Any:
        page = _build_html(title, traces, x_label, y_label, self.valves.chart_height_px)
        if self._can_embed():
            return HTMLResponse(content=page, headers={"Content-Disposition": "inline"})
        return _ascii_fallback(traces, title)

    # ─────────────────────────────────────────────────────────────────────────

    async def search_owid(
        self,
        query: str,
    ) -> str:
        """
        Search Our World in Data for charts matching a topic.

        Always call this first. Returns slugs and exact country names
        needed by the other tools.

        Args:
            query: Short plain-English topic, e.g. 'life expectancy',
                   'CO2 emissions', 'child mortality', 'GDP per capita'.
                   Avoid full sentences.
        """
        if search is None:
            return "Error: owid-catalog not installed. Run: pip install owid-catalog"
        try:
            results = search(query)
        except Exception as e:
            return f"Search error: {e}"

        if len(results) == 0:
            return (
                f"No charts found for '{query}'. "
                "Try broader terms, e.g. 'emissions' instead of 'carbon footprint by sector'."
            )

        n = min(len(results), self.valves.max_search_results)
        lines = [f"Found {n} chart(s) for '{query}':\n"]

        for i, res in enumerate(results[:n]):
            slug        = getattr(res, "slug", getattr(res, "path", "unknown"))
            title       = getattr(res, "title", "Untitled")
            description = getattr(res, "subtitle", "") or ""
            entities    = getattr(res, "available_entities", []) or []

            countries_str = (
                ", ".join(entities[:10])
                + (f" … (+{len(entities)-10} more)" if len(entities) > 10 else "")
                if entities else "not listed — try common country names"
            )

            lines.append(f"[{i+1}] {title}")
            lines.append(f"    slug:      {slug}")
            if description:
                lines.append(f"    about:     {description[:180]}")
            lines.append(f"    countries: {countries_str}")
            lines.append("")

        return "\n".join(lines)

    # ─────────────────────────────────────────────────────────────────────────

    async def chart_owid_data(
        self,
        slug: str,
        country: Optional[str] = None,
        year_start: Optional[Any] = None,
        year_end: Optional[Any] = None,
        chart_type: str = "line",
        value_column: Optional[str] = None,
    ) -> Any:
        """
        Fetch data for one country/entity and render an interactive Plotly chart.

        Prefer this over get_owid_data whenever the goal is a visualisation.
        Call search_owid first to get a valid slug.

        Args:
            slug:         Slug from search_owid, e.g. 'life-expectancy',
                          'co2-emissions-per-capita'. Copy exactly — do not guess.
            country:      Entity name from search_owid's countries list.
                          Examples: 'Italy', 'United States', 'South Korea'.
                          Omit to auto-select 'World' or the first available entity.
            year_start:   Start year inclusive, e.g. 1990.
            year_end:     End year inclusive, e.g. 2023.
            chart_type:   'line' (default), 'bar', or 'scatter'.
            value_column: Numeric column to plot. Leave blank for auto-detection.
        """
        if fetch is None:
            return "Error: owid-catalog not installed."

        try:
            df = _cached_fetch_df(slug).copy()
        except Exception as e:
            return f"Error fetching '{slug}': {e}"

        country_col, year_col = _detect_cols(df)
        if year_col is None:
            return f"Cannot chart '{slug}': no year column detected. Use get_owid_data to inspect."

        # Auto-pick entity
        chosen = country
        if not chosen and country_col:
            preferred = ["World", "world"]
            chosen = next((e for e in preferred if e in df[country_col].values), None)
            if not chosen and not df.empty:
                chosen = str(df[country_col].iloc[0])

        structural = {
            str(country_col).lower() if country_col else "",
            str(year_col).lower(), "code", "entity_code", "country_code",
        }
        val_col = _detect_value_col(df, structural, value_column)
        if val_col is None:
            return (
                f"Could not detect a numeric value column in '{slug}'.\n"
                f"Columns: {list(df.columns)}\nRetry with value_column='<name>'."
            )

        clean = _clean_series(df, country_col, year_col, val_col, chosen, year_start, year_end)
        if clean.empty:
            return (
                f"No data for '{chosen}' in '{slug}'.\n"
                f"Sample valid names: {_country_sample(df, country_col)}\n"
                "Check search_owid available_countries and retry."
            )

        chart_title = f"{slug.replace('-', ' ').title()} — {chosen or ''}"
        y_label     = str(val_col).replace("_", " ").title()

        trace = _make_trace(
            x=clean[year_col].tolist(),
            y=[float(v) for v in clean[val_col].tolist()],
            name=chosen or "",
            color=_SERIES_COLORS[0],
            chart_type=chart_type,
        )
        return self._render(chart_title, [trace], "Year", y_label)

    # ─────────────────────────────────────────────────────────────────────────

    async def compare_owid_countries(
        self,
        slug: str,
        countries: List[str],
        year_start: Optional[Any] = None,
        year_end: Optional[Any] = None,
        value_column: Optional[str] = None,
    ) -> Any:
        """
        Fetch data for 2–8 countries and overlay them on one chart.

        Use when the user wants to compare a metric across countries,
        e.g. "compare CO2 emissions in Italy, France and Germany since 1990".
        Call search_owid first for the slug and exact country names.
        """
        if fetch is None:
            return "Error: owid-catalog not installed."
        if not countries:
            return "Provide at least 2 country names."

        try:
            df = _cached_fetch_df(slug).copy()
        except Exception as e:
            return f"Error fetching '{slug}': {e}"

        country_col, year_col = _detect_cols(df)
        if year_col is None:
            return f"Cannot chart '{slug}': no year column found."

        structural = {
            str(country_col).lower() if country_col else "",
            str(year_col).lower(), "code", "entity_code", "country_code",
        }
        val_col = _detect_value_col(df, structural, value_column)
        if val_col is None:
            return (
                f"Could not detect a numeric value column.\n"
                f"Columns: {list(df.columns)}\nRetry with value_column='<name>'."
            )

        traces, missing = [], []

        for idx, country in enumerate(countries[:8]):
            clean = _clean_series(
                df.copy(), country_col, year_col, val_col,
                country, year_start, year_end,
            )
            if clean.empty:
                missing.append(country)
                continue
            traces.append(_make_trace(
                x=clean[year_col].tolist(),
                y=[float(v) for v in clean[val_col].tolist()],
                name=country,
                color=_SERIES_COLORS[idx % len(_SERIES_COLORS)],
                chart_type="line",
            ))

        if not traces:
            return (
                f"No data found for any of: {countries}.\n"
                f"Sample valid names: {_country_sample(df, country_col)}"
            )

        chart_title = f"{slug.replace('-', ' ').title()} — Comparison"
        y_label     = str(val_col).replace("_", " ").title()
        result      = self._render(chart_title, traces, "Year", y_label)

        if missing and isinstance(result, str):
            result += f"\n\n> Warning: no data for {', '.join(missing)}"

        return result

    # ─────────────────────────────────────────────────────────────────────────

    async def get_owid_data(
        self,
        slug: str,
        country: Optional[str] = None,
        year_start: Optional[Any] = None,
        year_end: Optional[Any] = None,
        columns: Optional[List[str]] = None,
    ) -> str:
        """
        Fetch raw OWID data as a plain-text table.

        Use only when the user explicitly needs numbers.
        For any visualisation use chart_owid_data or compare_owid_countries.

        Args:
            slug: Slug from search_owid.
            country: Optional country name.
            year_start: Optional start year/date.
            year_end: Optional end year/date.
            columns: Specific columns to include. Leave blank to discover available columns if the dataset is large.
        """
        if fetch is None:
            return "Error: owid-catalog not installed."

        try:
            df = _cached_fetch_df(slug).copy()
        except Exception as e:
            return f"Error fetching '{slug}': {e}"

        country_col, year_col = _detect_cols(df)

        if country and country_col:
            available = df[country_col].dropna().unique().tolist()
            matched_country = _fuzzy_match_country(country, available)
            df = df[df[country_col].astype(str) == matched_country]

        if year_col:
            df = df.copy()
            is_date = df[year_col].dtype.kind == 'M' or df[year_col].astype(str).str.match(r'^\d{4}-\d{2}-\d{2}').any()
            if is_date:
                df[year_col] = pd.to_datetime(df[year_col], errors="coerce")
            else:
                df[year_col] = pd.to_numeric(df[year_col], errors="coerce")
            df = df.dropna(subset=[year_col])
            if not is_date:
                df[year_col] = df[year_col].astype(int)
            if year_start is not None:
                start_val = pd.to_datetime(year_start) if is_date else float(year_start)
                df = df[df[year_col] >= start_val]
            if year_end is not None:
                end_val = pd.to_datetime(year_end) if is_date else float(year_end)
                df = df[df[year_col] <= end_val]
            df = df.sort_values(year_col)

        if df.empty:
            return (
                f"No data for '{country}' in '{slug}'.\n"
                f"Sample valid names: {_country_sample(_cached_fetch_df(slug), country_col)}"
            )

        if columns:
            missing_cols = [c for c in columns if c not in df.columns]
            if missing_cols:
                return f"Error: Columns not found: {missing_cols}\nAvailable columns: {list(df.columns)}"
            cols_to_keep = list(columns)
            if country_col and country_col not in cols_to_keep:
                cols_to_keep.insert(0, country_col)
            if year_col and year_col not in cols_to_keep:
                cols_to_keep.insert(1, year_col)
            # Remove duplicates preserving order
            seen = set()
            cols_to_keep = [x for x in cols_to_keep if not (x in seen or seen.add(x))]
            df = df[cols_to_keep]
        elif len(df.columns) > 5:
            return (
                f"Dataset has {len(df.columns)} columns.\n"
                f"Available columns: {list(df.columns)}\n\n"
                f"Please specify a list of 'columns' to view (e.g. ['total_cases', 'new_deaths'])."
            )

        cap = self.valves.max_table_rows
        df_subset = df.head(cap)

        # Build simple markdown table
        headers = list(df_subset.columns)
        header_row = "| " + " | ".join(headers) + " |"
        separator_row = "| " + " | ".join(["---"] * len(headers)) + " |"
        
        data_rows = []
        for _, row in df_subset.iterrows():
            data_rows.append("| " + " | ".join(str(x) for x in row.values) + " |")
        
        md_table = "\n".join([header_row, separator_row] + data_rows)

        return (
            f"Data: {slug} | {country or 'all entities'} | "
            f"showing {min(len(df), cap)} of {len(df)} rows\n\n"
            f"{md_table}"
        )
