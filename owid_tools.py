"""
title: OWID Data Tools
author: Marco
version: 2.2.0
license: MIT
description: Search and fetch Our World in Data datasets, embed official
             interactive charts or return raw tables.
required_open_webui_version: 0.4.0

  ## Search & Data Access Workflow
  1. search_owid(query) -> returns slugs and brief descriptions.
  2. chart_owid_data(slug, ...) OR get_owid_data(slug, ...) -> fetches data.
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



# ── Helpers ───────────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=16)
def _cached_fetch_df(slug: str) -> pd.DataFrame:
    """Fetch from OWID catalog and cache the resulting DataFrame."""
    if fetch is None:
        raise RuntimeError("owid-catalog not installed.")
    table = fetch(slug)
    return pd.DataFrame(table).reset_index()

@functools.lru_cache(maxsize=32)
def _cached_search(q: str):
    """Cache OWID catalog searches."""
    if search is None:
        return []
    return search(q)
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

    # Keep only the two columns we need to minimize memory and copy cost
    df = df[[year_col, val_col]].copy()

    is_date = pd.api.types.is_datetime64_any_dtype(df[year_col])
    if not is_date:
        first_valid = df[year_col].dropna().iloc[0] if not df[year_col].dropna().empty else None
        if first_valid is not None and isinstance(first_valid, str) and len(first_valid) >= 10:
            import re
            is_date = bool(re.match(r'^\d{4}-\d{2}-\d{2}', str(first_valid)))

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

    df[val_col] = pd.to_numeric(df[val_col], errors="coerce")

    # Deduplicate: multiple source rows per year -> take mean
    df = df.groupby(year_col, as_index=False)[val_col].mean()

    return df.sort_values(year_col).reset_index(drop=True)

_SERIES_COLORS = [
    "#2082a2", "#bf1b1b", "#588a0f", "#ca6f34",
    "#0c6947", "#2774c6", "#009655", "#ab348a",
    "#eb6400", "#17393d", "#660000", "#1b0655",
    "#cc235c", "#253f77", "#0089be", "#af488f",
]
_PLOTLY_CDN = "2.27.0"
_BG    = "#ffffff"
_PANEL = "#f9f9f9"
_TEXT  = "#002147"
_MUTED = "#426591"
_BORDER = "#e0e0e0"
_GRID  = "#dadada"
_FONT  = "Lato, 'Helvetica Neue', Helvetica, Arial, sans-serif"
def _validate_layout(config: dict) -> dict:
    allowed = {
        "xaxis", "yaxis", "legend", "annotations", "shapes",
        "margin", "title", "hovermode", "hoverlabel",
    }
    def _is_safe(k, v):
        if k not in allowed: return False
        if isinstance(v, dict):
            for nk, nv in v.items():
                if not _is_safe(nk, nv): return False
        return True
    return {k: v for k, v in config.items() if _is_safe(k, v)}

def _build_html(title: str, traces: list, x_label: str, y_label: str, height: int = 460, extra_layout: Optional[dict] = None) -> str:
    layout = {
        "title": {"text": title, "font": {"size": 18, "color": _TEXT, "family": "'Playfair Display', Georgia, serif"}, "x": 0.04},
        "paper_bgcolor": _BG, "plot_bgcolor": _BG,
        "font": {"color": _TEXT, "family": _FONT},
        "margin": {"l": 72, "r": 24, "t": 60, "b": 52},
        "legend": {"bgcolor": "rgba(255,255,255,0.8)", "bordercolor": _BORDER, "borderwidth": 1, "font": {"color": _TEXT, "size": 12}},
        "xaxis": {"gridcolor": _GRID, "linecolor": _TEXT, "tickcolor": _TEXT, "tickformat": "d", "title": {"text": x_label, "font": {"size": 13, "color": _MUTED}}, "tickfont": {"color": _MUTED}},
        "yaxis": {"gridcolor": _GRID, "linecolor": _TEXT, "tickcolor": _TEXT, "title": {"text": y_label, "font": {"size": 13, "color": _MUTED}}, "tickfont": {"color": _MUTED}, "zeroline": True, "zerolinecolor": _GRID, "tickformat": "~s"},
        "hovermode": "x unified",
        "hoverlabel": {"bgcolor": _BG, "bordercolor": _BORDER, "font": {"color": _TEXT, "size": 13, "family": _FONT}},
    }
    if extra_layout:
        layout.update(_validate_layout(extra_layout))

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{{html.escape(title)}}</title>
<link href="https://fonts.googleapis.com/css2?family=Lato:ital,wght@0,400;0,700;1,400&family=Playfair+Display:wght@400;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box}}
html,body{{margin:0;padding:0;width:100%;background:{_BG};color:{_TEXT};font-family:{_FONT};font-size:14px;overflow:visible;}}
#chart{{width:100%;height:{height}px;padding:12px 12px 4px}}
</style>
</head><body>
<div id="chart"></div>
<script src="https://cdn.plot.ly/plotly-{_PLOTLY_CDN}.min.js"></script>
<script>
(function(){{
  var traces = {json.dumps(traces)};
  var layout = {json.dumps(layout)};
  var config = {{responsive: true, displayModeBar: true, displaylogo: false}};
  Plotly.newPlot("chart", traces, layout, config);
  function syncHeight(){{
    var h = document.documentElement.scrollHeight;
    try{{ if(window.frameElement) window.frameElement.style.height = h + "px"; }}catch(e){{}}
  }}
  window.addEventListener("load", syncHeight);
  if(typeof ResizeObserver !== "undefined") new ResizeObserver(syncHeight).observe(document.body);
  [300, 900].forEach(function(t){{ setTimeout(syncHeight, t); }});
}})();
</script></body></html>"""

def _ascii_fallback(traces: list, title: str) -> str:
    h = 10
    lines = [f"### {title}", ""]
    for t in traces:
        y = [v for v in t.get("y", []) if v is not None]
        x = t.get("x", [])
        name = t.get("name", "")
        if not y: continue
        max_y, min_y = max(y), min(y)
        rng = max_y - min_y or 1.0
        grid = [[" "] * (len(y) * 2) for _ in range(h)]
        for i, v in enumerate(y):
            r = int((max_y - v) / rng * (h - 1))
            r = max(0, min(h - 1, r))
            grid[r][i * 2] = "●"
        if name: lines.append(f"**{name}**")
        lines.append("```text")
        for i, row in enumerate(grid):
            thr = max_y - (rng * i / (h - 1)) if h > 1 else max_y
            lines.append(f"{thr:9.2f} |{''.join(row)}")
        lines.append("         +" + "--" * len(y))
        lines.append("           " + "".join(f"{str(x[i])[:4]:4} " for i in range(min(len(x), 12))))
        lines.append("```\\n")
    return "\\n".join(lines)

def _make_trace(x: list, y: list, name: str, color: str, chart_type: str = "line") -> dict:
    ct = chart_type.lower().strip()
    if ct == "bar":
        return {"type": "bar", "name": name, "x": x, "y": y, "marker": {"color": color}}
    return {
        "type": "scatter", "mode": "lines+markers" if ct == "line" else "markers",
        "name": name, "x": x, "y": y, "connectgaps": False,
        "line": {"color": color, "width": 2}, "marker": {"size": 4, "color": color}
    }

def _detect_value_col(df: pd.DataFrame, structural: set, override: Optional[str]):
    if override and override in df.columns: return override
    return next((c for c in df.columns if str(c).lower() not in structural and pd.api.types.is_numeric_dtype(df[c])), None)

# ─────────────────────────────────────────────────────────────────────────────

class Tools:
    """
    OWID Data Tools — search, fetch, chart, and compare Our World in Data.

    Workflow:
      1. search_owid   → discover slugs + valid country names
      2. chart_owid_data → embed official OWID interactive chart (Recommended)
         custom_chart or compare_owid_countries → generate custom Plotly analysis charts
         get_owid_data → raw table (only when numbers are explicitly needed)
    """

    def __init__(self) -> None:
        self.valves = self.Valves()

    class Valves(BaseModel):
        allow_iframe_embedding: bool = Field(
            True,
            description="Allow embedding OWID iframes. If false, returns direct links.",
        )
        chart_height_px: int = Field(460, description="Chart height in pixels.")
        max_table_rows: int  = Field(20,  description="Max rows from get_owid_data.")
        max_search_results: int = Field(5, description="Max results from search_owid.")
    def _can_embed(self) -> bool:
        return self.valves.allow_iframe_embedding and HTMLResponse is not None
    def _render(self, title: str, traces: list, x_label: str, y_label: str, extra_layout: Optional[dict] = None) -> Any:
        page = _build_html(title, traces, x_label, y_label, self.valves.chart_height_px, extra_layout=extra_layout)
        if self._can_embed():
            return HTMLResponse(content=page, headers={"Content-Disposition": "inline"})  # type: ignore
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

        import asyncio
        
        search_terms = [query]
        if "," in query:
            search_terms.extend([q.strip() for q in query.split(',') if q.strip()])
        else:
            words = [w for w in query.split() if len(w) > 3]
            if len(words) > 1:
                search_terms.extend(words)

        seen_terms = set()
        unique_terms = [t for t in search_terms if t.lower() not in seen_terms and not seen_terms.add(t.lower())]

        async def _run_search(q: str):
            try:
                return await asyncio.to_thread(_cached_search, q)
            except Exception:
                return []

        search_tasks = [_run_search(q) for q in unique_terms[:5]] # Max 5 concurrent searches
        results_lists = await asyncio.gather(*search_tasks)

        seen_slugs = set()
        results = []
        for res_list in results_lists:
            for res in res_list:
                slug = getattr(res, "slug", getattr(res, "path", "unknown"))
                if slug not in seen_slugs:
                    seen_slugs.add(slug)
                    results.append(res)

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

    async def chart_owid_data(
        self,
        slug: str,
        tab: Optional[str] = None,
        countries: Optional[List[str]] = None,
        time: Optional[str] = None,
        **kwargs
    ) -> Any:
        """
        Embed the official Our World in Data interactive grapher.
        
        This provides the most complete and authentic OWID visualisation,
        often including maps, multi-country comparisons, and rich metadata.

        Args:
            slug: EXACT slug returned by search_owid. Do NOT guess or hallucinate.
            tab: Optional chart view ('chart', 'map', or 'table').
            countries: Optional list of specific country names to pre-filter in the chart.
            time: Optional time range, e.g., '2020' or '1990..2020'.
        """
        url = f"https://ourworldindata.org/grapher/{slug}"
        
        query_params = []
        if tab:
            query_params.append(f"tab={tab}")
        if countries:
            import urllib.parse
            country_str = "~".join(countries)
            query_params.append(f"country={urllib.parse.quote(country_str)}")
        if time:
            query_params.append(f"time={time}")
            
        if query_params:
            url += "?" + "&".join(query_params)
            
        height = self.valves.chart_height_px + 140  # Official grapher needs more space for controls
        html_content = f"""
        <iframe src="{url}" loading="lazy" style="width: 100%; height: {height}px; border: 0px none; border-radius: 4px;"></iframe>
        <div style="text-align: center; margin-top: 8px; font-family: system-ui, -apple-system, sans-serif;">
            <a href="{url}" target="_blank" style="color: #94a3b8; text-decoration: none; font-size: 13px;">
                View on Our World in Data ↗
            </a>
        </div>
        """
        if self._can_embed():
            return HTMLResponse(content=html_content, headers={"Content-Disposition": "inline"})  # type: ignore
        return f"External embedding disabled (CDN off). View official chart here: {url}"

    async def custom_chart(
        self,
        slug: str,
        country: Optional[str] = None,
        year_start: Optional[Any] = None,
        year_end: Optional[Any] = None,
        custom_js_config: Optional[dict] = None,
        value_column: Optional[str] = None,
        chart_type: str = "line",
    ) -> Any:
        """
        Fetch data and render a chart with custom Plotly configuration.
        Use this for custom data analysis that isn't satisfied by the official embed.

        Args:
            slug: EXACT slug returned by search_owid. Do NOT guess or hallucinate.
        """
        if fetch is None: return "Error: owid-catalog not installed."
        try: df = _cached_fetch_df(slug).copy()
        except Exception as e: return f"Error fetching '{slug}': {e}"
        country_col, year_col = _detect_cols(df)
        structural = {str(country_col).lower() if country_col else "", str(year_col).lower(), "code", "entity_code", "country_code"}
        val_col = _detect_value_col(df, structural, value_column)
        if val_col is None: return f"Could not detect a numeric value column in '{slug}'."
        clean = _clean_series(df, country_col, year_col, val_col, country, year_start, year_end)
        if clean.empty: return "No data found."
        trace = _make_trace(x=clean[year_col].tolist(), y=[float(v) for v in clean[val_col].tolist()], name=country or "Data", color=_SERIES_COLORS[0], chart_type=chart_type)
        return self._render(f"{slug} custom analysis", [trace], "Year", str(val_col), extra_layout=custom_js_config)

    async def compare_owid_countries(
        self,
        slug: str,
        countries: List[str],
        year_start: Optional[Any] = None,
        year_end: Optional[Any] = None,
        value_column: Optional[str] = None,
    ) -> Any:
        """
        Fetch data for 2–8 countries and overlay them on one custom chart.
        
        Args:
            slug: EXACT slug returned by search_owid. Do NOT guess or hallucinate.
        """
        if fetch is None: return "Error: owid-catalog not installed."
        if not countries: return "Provide at least 2 country names."
        try: df = _cached_fetch_df(slug).copy()
        except Exception as e: return f"Error fetching '{slug}': {e}"
        country_col, year_col = _detect_cols(df)
        structural = {str(country_col).lower() if country_col else "", str(year_col).lower(), "code", "entity_code", "country_code"}
        val_col = _detect_value_col(df, structural, value_column)
        if val_col is None: return "Could not detect a numeric value column."
        traces, missing = [], []
        for idx, country in enumerate(countries[:8]):
            clean = _clean_series(df, country_col, year_col, val_col, country, year_start, year_end)
            if clean.empty:
                missing.append(country)
                continue
            traces.append(_make_trace(x=clean[year_col].tolist(), y=[float(v) for v in clean[val_col].tolist()], name=country, color=_SERIES_COLORS[idx % len(_SERIES_COLORS)], chart_type="line"))
        if not traces: return f"No data found for any of: {countries}."
        chart_title = f"{slug.replace('-', ' ').title()} — Comparison"
        y_label = str(val_col).replace("_", " ").title()
        result = self._render(chart_title, traces, "Year", y_label)
        if missing and isinstance(result, str): result += f"\\n\\n> Warning: no data for {', '.join(missing)}"
        return result

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
        For any visualisation use chart_owid_data.

        Args:
            slug: EXACT slug returned by search_owid. Do NOT guess or hallucinate.
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
            is_date = pd.api.types.is_datetime64_any_dtype(df[year_col])
            if not is_date:
                first_valid = df[year_col].dropna().iloc[0] if not df[year_col].dropna().empty else None
                if first_valid is not None and isinstance(first_valid, str) and len(first_valid) >= 10:
                    import re
                    is_date = bool(re.match(r'^\d{4}-\d{2}-\d{2}', str(first_valid)))
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
