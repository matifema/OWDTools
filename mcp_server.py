"""
OWID Data Tools — MCP Server

A remote MCP server that wraps the existing owid_tools.py plugin logic,
exposing search, data fetching, and charting capabilities via the
Model Context Protocol using FastMCP with Streamable HTTP transport.

Run:
    python mcp_server.py

The server will start on https://aaaa.tail4ffb78.ts.net/mcp
"""

from __future__ import annotations

import os
from typing import Any, List, Optional

from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

# Import helper functions and constants from the existing plugin.
# The OpenWebUI-specific imports (HTMLResponse) will gracefully degrade to None.
from owid_tools import (
    _cached_fetch_df,
    _cached_search,
    _detect_cols,
    _fuzzy_match_country,
    _clean_series,
    _detect_value_col,
    _make_trace,
    _country_sample,
    _SERIES_COLORS,
)

# ---------------------------------------------------------------------------
# Configuration from environment variables
# ---------------------------------------------------------------------------
MAX_TABLE_ROWS = int(os.environ.get("MAX_TABLE_ROWS", "20"))
MAX_SEARCH_RESULTS = int(os.environ.get("MAX_SEARCH_RESULTS", "5"))
PORT = int(os.environ.get("PORT", "8000"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")  # e.g. https://myserver.com:8000 — LLM uses this for fetch()

# ---------------------------------------------------------------------------
# FastMCP server instance
# ---------------------------------------------------------------------------
mcp = FastMCP("OWID Data Tools")

# CORS middleware is injected at the entry point via mcp.run_http_async().


    # --- REST API Routes ---

@mcp.custom_route("/api/data/{slug}", methods=["GET"])
async def rest_get_data(request):
    from starlette.responses import JSONResponse as StarletteJSONResponse
    import pandas as pd
    from urllib.parse import unquote

    # Parse path params
    slug = request.path_params.get("slug", "")
    query = dict(request.query_params)

    year = int(query["year"]) if "year" in query else None
    year_start = int(query["year_start"]) if "year_start" in query else None
    year_end = int(query["year_end"]) if "year_end" in query else None
    country = query.get("country")
    limit = int(query.get("limit", "2000"))

    try:
        df = _cached_fetch_df(slug).copy()
    except Exception as e:
        return StarletteJSONResponse({"error": str(e)}, status_code=404)

    country_col, year_col = _detect_cols(df)

    if country and country_col:
        available = df[country_col].dropna().unique().tolist()
        matched = _fuzzy_match_country(country, available)
        df = df[df[country_col].astype(str) == matched]

    if year_col:
        df[year_col] = pd.to_numeric(df[year_col], errors="coerce")
        df = df.dropna(subset=[year_col])
        df[year_col] = df[year_col].astype(int)
        if year:
            df = df[df[year_col] == year]
        else:
            if year_start:
                df = df[df[year_col] >= year_start]
            if year_end:
                df = df[df[year_col] <= year_end]

    df = df.dropna(how="all")
    return StarletteJSONResponse(df.head(limit).to_dict(orient="records"))


@mcp.custom_route("/api/schema/{slug}", methods=["GET"])
async def rest_get_schema(request):
    from starlette.responses import JSONResponse as StarletteJSONResponse
    import pandas as pd

    slug = request.path_params.get("slug", "")

    try:
        df = _cached_fetch_df(slug)
    except Exception as e:
        return StarletteJSONResponse({"error": str(e)}, status_code=404)

    schema = {}
    for col in df.columns:
        info = {"dtype": str(df[col].dtype)}
        if pd.api.types.is_numeric_dtype(df[col]):
            info["min"] = round(float(df[col].min()), 4)
            info["max"] = round(float(df[col].max()), 4)
            info["nulls"] = int(df[col].isna().sum())
        else:
            info["unique_count"] = int(df[col].nunique())
            info["sample"] = df[col].dropna().unique()[:5].tolist()
        schema[col] = info
    return StarletteJSONResponse(schema)


# ---------------------------------------------------------------------------
# Helper: import check
# ---------------------------------------------------------------------------
def _check_owid_catalog() -> None:
    """Raise RuntimeError if owid-catalog is not installed."""
    try:
        from owid.catalog import search, fetch  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "owid-catalog is not installed. Install it with: pip install owid-catalog"
        )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool
async def search_owid(query: str) -> str:
    """
    Search Our World in Data for charts matching a topic.

    Always call this first. Returns slugs and exact country names
    needed by the other tools.

    Args:
        query: Short plain-English topic, e.g. 'life expectancy',
               'CO2 emissions', 'child mortality', 'GDP per capita'.
               Avoid full sentences.
    """
    try:
        _check_owid_catalog()
    except RuntimeError as e:
        return str(e)

    import asyncio

    search_terms = [query]
    if "," in query:
        search_terms.extend([q.strip() for q in query.split(",") if q.strip()])
    else:
        words = [w for w in query.split() if len(w) > 3]
        if len(words) > 1:
            search_terms.extend(words)

    seen_terms: set[str] = set()
    unique_terms = [
        t
        for t in search_terms
        if t.lower() not in seen_terms and not seen_terms.add(t.lower())
    ]

    async def _run_search(q: str):
        try:
            return await asyncio.to_thread(_cached_search, q)
        except Exception:
            return []

    search_tasks = [_run_search(q) for q in unique_terms[:5]]
    results_lists = await asyncio.gather(*search_tasks)

    seen_slugs: set[str] = set()
    results = []
    for res_list in results_lists:
        for res in res_list:
            slug = getattr(res, "slug", getattr(res, "path", "unknown"))
            if slug not in seen_slugs:
                seen_slugs.add(slug)
                results.append(res)

    if not results:
        return (
            f"No charts found for '{query}'. "
            "Try broader terms, e.g. 'emissions' instead of 'carbon footprint by sector'."
        )

    n = min(len(results), MAX_SEARCH_RESULTS)
    lines = [f"Found {n} chart(s) for '{query}':\n"]

    for i, res in enumerate(results[:n]):
        slug = getattr(res, "slug", getattr(res, "path", "unknown"))
        title = getattr(res, "title", "Untitled")
        description = getattr(res, "subtitle", "") or ""
        entities = getattr(res, "available_entities", []) or []

        countries_str = (
            ", ".join(entities[:10])
            + (f" ... (+{len(entities) - 10} more)" if len(entities) > 10 else "")
            if entities
            else "not listed -- try common country names"
        )

        lines.append(f"[{i + 1}] {title}")
        lines.append(f"    slug:      {slug}")
        if description:
            lines.append(f"    about:     {description[:180]}")
        lines.append(f"    countries: {countries_str}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool
async def get_owid_data(
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
    import pandas as pd

    try:
        _check_owid_catalog()
    except RuntimeError as e:
        return str(e)

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
            first_valid = (
                df[year_col].dropna().iloc[0]
                if not df[year_col].dropna().empty
                else None
            )
            if (
                first_valid is not None
                and isinstance(first_valid, str)
                and len(first_valid) >= 10
            ):
                import re

                is_date = bool(re.match(r"^\d{4}-\d{2}-\d{2}", str(first_valid)))
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
        seen: set[str] = set()
        cols_to_keep = [x for x in cols_to_keep if not (x in seen or seen.add(x))]
        df = df[cols_to_keep]
    elif len(df.columns) > 5:
        return (
            f"Dataset has {len(df.columns)} columns.\n"
            f"Available columns: {list(df.columns)}\n\n"
            f"Please specify a list of 'columns' to view (e.g. ['total_cases', 'new_deaths'])."
        )

    cap = MAX_TABLE_ROWS
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


@mcp.tool
async def chart_owid_data(
    slug: str,
    tab: Optional[str] = None,
    countries: Optional[List[str]] = None,
    time: Optional[str] = None,
) -> str:
    """
    Get the URL for the official Our World in Data interactive grapher chart.

    This provides the most complete and authentic OWID visualisation,
    often including maps, multi-country comparisons, and rich metadata.

    Args:
        slug: EXACT slug returned by search_owid. Do NOT guess or hallucinate.
        tab: Optional chart view ('chart', 'map', or 'table').
        countries: Optional list of specific country names to pre-filter in the chart.
        time: Optional time range, e.g., '2020' or '1990..2020'.
    """
    import urllib.parse

    url = f"https://ourworldindata.org/grapher/{slug}"

    query_params = []
    if tab:
        query_params.append(f"tab={tab}")
    if countries:
        try:
            df = _cached_fetch_df(slug)
            country_col, _ = _detect_cols(df)
            if country_col:
                available = df[country_col].dropna().unique().tolist()
                countries = [_fuzzy_match_country(c, available) for c in countries]
        except Exception:
            pass
        country_str = "~".join(countries)
        query_params.append(f"country={urllib.parse.quote(country_str)}")
        if tab == "map":
            # The map view specifically uses mapSelect to highlight countries
            map_select_str = "~" + "~".join(countries)
            query_params.append(f"mapSelect={urllib.parse.quote(map_select_str)}")
    if time:
        query_params.append(f"time={time}")

    if query_params:
        url += "?" + "&".join(query_params)

    return url


@mcp.tool
async def custom_chart(
    slug: str,
    countries: Optional[List[str]] = None,
    year_start: Optional[Any] = None,
    year_end: Optional[Any] = None,
    value_column: Optional[str] = None,
    chart_type: str = "line",
) -> str:
    """
    Get a URL to the official OWID grapher with optional country and time filtering.

    This builds an OWID grapher URL with country and time parameters applied,
    similar to chart_owid_data. Use this when you need a direct link to view
    data for specific countries or time ranges.

    Args:
        slug: EXACT slug returned by search_owid. Do NOT guess or hallucinate.
        countries: Optional list of country names to highlight.
        year_start: Optional start year (used for time parameter).
        year_end: Optional end year (used for time parameter).
        value_column: Optional value column name (unused for URL, kept for compatibility).
        chart_type: Chart type ('line', 'bar', 'area', 'scatter') — unused for URL.
    """
    import urllib.parse

    url = f"https://ourworldindata.org/grapher/{slug}"

    query_params = []
    if countries:
        try:
            df = _cached_fetch_df(slug)
            country_col, _ = _detect_cols(df)
            if country_col:
                available = df[country_col].dropna().unique().tolist()
                countries = [_fuzzy_match_country(c, available) for c in countries]
        except Exception:
            pass
        country_str = "~".join(countries)
        query_params.append(f"country={urllib.parse.quote(country_str)}")

    # Build time parameter from year_start/year_end
    if year_start is not None or year_end is not None:
        time_parts = []
        if year_start is not None:
            time_parts.append(str(year_start))
        if year_end is not None:
            time_parts.append(str(year_end))
        time_param = "..".join(time_parts)
        if time_param:
            query_params.append(f"time={time_param}")

    if query_params:
        url += "?" + "&".join(query_params)

    return url



@mcp.tool
async def get_owid_data_json(
    slug: str,
    country: Optional[str] = None,
    year_start: Optional[Any] = None,
    year_end: Optional[Any] = None,
    columns: Optional[List[str]] = None,
    limit: int = 100,
) -> str:
    """
    Returns OWID data as minified JSON for direct embedding in code artifacts.
    Use instead of get_owid_data when generating visualizations or show_widget charts.
    Keep limit low (≤100) to avoid blowing up context. For full datasets, use
    the REST endpoint (see get_dataset_schema) in a fetch() call instead.

    Args:
        slug: EXACT slug returned by search_owid.
        country: Optional country filter.
        year_start: Optional start year.
        year_end: Optional end year.
        columns: Specific columns to include.
        limit: Max rows to return (default 100).
    """
    import pandas as pd
    import json

    try:
        _check_owid_catalog()
    except RuntimeError as e:
        return str(e)

    try:
        df = _cached_fetch_df(slug).copy()
    except Exception as e:
        return json.dumps({"error": str(e)})

    country_col, year_col = _detect_cols(df)

    if country and country_col:
        available = df[country_col].dropna().unique().tolist()
        matched = _fuzzy_match_country(country, available)
        df = df[df[country_col].astype(str) == matched]

    if year_col:
        df[year_col] = pd.to_numeric(df[year_col], errors="coerce")
        df = df.dropna(subset=[year_col])
        df[year_col] = df[year_col].astype(int)
        if year_start is not None:
            df = df[df[year_col] >= int(year_start)]
        if year_end is not None:
            df = df[df[year_col] <= int(year_end)]
        df = df.sort_values(year_col)

    if columns:
        missing = [c for c in columns if c not in df.columns]
        if missing:
            return json.dumps({"error": f"Columns not found: {missing}", "available": list(df.columns)})
        keep = list(dict.fromkeys(
            ([country_col] if country_col and country_col not in columns else []) +
            ([year_col] if year_col and year_col not in columns else []) +
            list(columns)
        ))
        df = df[keep]

    df = df.dropna(how="all")
    return json.dumps(df.head(limit).to_dict(orient="records"), default=str)


@mcp.tool
async def get_dataset_schema(slug: str) -> str:
    """
    Returns column names, data types, and numeric bounds for an OWID dataset.
    Call this before building any chart or show_widget visualization to get
    correct axis ranges, column names, and data types without fetching rows.
    Also returns the REST endpoint URL to use in fetch() calls from show_widget.

    Args:
        slug: EXACT slug returned by search_owid.
    """
    import pandas as pd
    import json

    try:
        _check_owid_catalog()
    except RuntimeError as e:
        return str(e)

    try:
        df = _cached_fetch_df(slug)
    except Exception as e:
        return json.dumps({"error": str(e)})

    schema = {}
    for col in df.columns:
        info = {"dtype": str(df[col].dtype)}
        if pd.api.types.is_numeric_dtype(df[col]):
            info["min"] = round(float(df[col].min()), 4)
            info["max"] = round(float(df[col].max()), 4)
            info["nulls"] = int(df[col].isna().sum())
        else:
            info["unique_count"] = int(df[col].nunique())
            info["sample"] = df[col].dropna().unique()[:5].tolist()
        schema[col] = info

    return json.dumps({
        "slug": slug,
        "total_rows": len(df),
        "columns": schema,
        "rest_endpoint": f"{PUBLIC_URL}/api/data/{slug}",
        "schema_endpoint": f"{PUBLIC_URL}/api/schema/{slug}",
    }, indent=2)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
import asyncio

async def _main():
    await mcp.run_http_async(
        transport="http",
        host="0.0.0.0",
        port=PORT,
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["GET"],
                allow_headers=["*"],
            )
        ],
    )

if __name__ == "__main__":
    asyncio.run(_main())
