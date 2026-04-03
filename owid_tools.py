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

# ─────────────────────────────────────────────────────────────────────────────

class Tools:
    """
    OWID Data Tools — search, fetch, chart, and compare Our World in Data.

    Workflow:
      1. search_owid   → discover slugs + valid country names
      2. chart_owid_data → embed official OWID interactive chart (Recommended)
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
        **kwargs
    ) -> Any:
        """
        Embed the official Our World in Data interactive grapher.
        
        This provides the most complete and authentic OWID visualisation,
        often including maps, multi-country comparisons, and rich metadata.

        Args:
            slug: Slug from search_owid, e.g. 'life-expectancy'.
            tab: Optional chart view ('chart', 'map', or 'table').
            countries: Optional list of specific country names to pre-filter in the chart.
        """
        url = f"https://ourworldindata.org/grapher/{slug}"
        
        query_params = []
        if tab:
            query_params.append(f"tab={tab}")
        if countries:
            import urllib.parse
            country_str = "~".join(countries)
            query_params.append(f"country={urllib.parse.quote(country_str)}")
            
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
