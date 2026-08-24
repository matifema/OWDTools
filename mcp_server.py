"""
OWID Data Tools — MCP Server

A remote MCP server that wraps the OWID tools plugin logic, exposing search,
data fetching, and charting capabilities via the Model Context Protocol.

The server uses Streamable HTTP transport (the transport supported by both
Gemini and Claude remote MCP connections) and can be protected with either:

  * no auth        (MCP_AUTH=none, default) — OWID data is public, so no token is
                    needed; recommended unless you want to protect your endpoint
  * a static key   (MCP_AUTH=api_key)      — Gemini "API key" auth
  * OAuth 2.1      (MCP_AUTH=oauth)        — optional sign-in for claude.ai connectors

Run:
    python mcp_server.py

Environment variables:
    MCP_AUTH           none | api_key | oauth   (default: none)
    MCP_API_KEY        static bearer token when MCP_AUTH=api_key
    PUBLIC_URL         public HTTPS base URL, e.g. https://your-app.example.com
                       (required for oauth; used for REST endpoint links)
    HOST / PORT        bind address (default 0.0.0.0:8000)
    MCP_PATH           MCP endpoint path (default /mcp)
    MAX_TABLE_ROWS     default max rows for raw data responses
    MAX_SEARCH_RESULTS default max search results

MCP endpoint (add this URL in Gemini / Claude):
    {PUBLIC_URL}/mcp
"""

from __future__ import annotations

import html
import json
import os
import urllib.parse
from typing import Any, List, Optional

import pandas as pd
from fastmcp import FastMCP
from mcp.server.auth.settings import ClientRegistrationOptions
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

# Import helper functions and constants from the OpenWebUI tool plugin.
# The OpenWebUI-specific imports (HTMLResponse) degrade gracefully to None there.
from owid_tools import (
    _BG,
    _BORDER,
    _FONT,
    _GRID,
    _MUTED,
    _PLOTLY_CDN,
    _SERIES_COLORS,
    _TEXT,
    _build_html,
    _cached_fetch_df,
    _cached_search,
    _detect_cols,
    _detect_value_col,
    _fuzzy_match_country,
    _make_trace,
)

# ---------------------------------------------------------------------------
# Configuration from environment variables
# ---------------------------------------------------------------------------
MAX_TABLE_ROWS = int(os.environ.get("MAX_TABLE_ROWS", "20"))
MAX_SEARCH_RESULTS = int(os.environ.get("MAX_SEARCH_RESULTS", "5"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
MCP_PATH = os.environ.get("MCP_PATH", "/mcp").strip()
if not MCP_PATH.startswith("/"):
    MCP_PATH = "/" + MCP_PATH
AUTH_MODE = os.environ.get("MCP_AUTH", "none").strip().lower()
# Public HTTPS base URL, e.g. https://your-app.example.com (no trailing slash).
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

_INSTRUCTIONS = """
You have tools to explore Our World in Data (OWID).

Workflow:
  1. search_owid(query) to discover exact slugs and valid entity names.
  2. For a chart: use generate_chart_html (works in sandboxed iframes such as
     claude.ai) or get_dataset_schema + generate_chart_scaffold for a
     client-side template.
  3. For raw numbers: get_owid_data_json (small) or the REST endpoint returned
     by get_dataset_schema (large datasets).

Always use the exact slug returned by search_owid — never guess slugs.
"""


def _public_base(request=None) -> str:
    """Public base URL for this server.

    Prefers the PUBLIC_URL env var. When unset, infers it from the incoming
    request (Host + X-Forwarded-Proto headers) so deployments behind
    Cloudflare, tunnels, or proxies work with zero configuration.
    """
    if PUBLIC_URL:
        return PUBLIC_URL
    try:
        if request is None:
            from fastmcp.server.dependencies import get_http_request

            request = get_http_request()
        if request is not None:
            host = request.headers.get("host", "")
            scheme = request.headers.get("x-forwarded-proto", "https")
            if host:
                return f"{scheme}://{host}"
    except Exception:
        pass
    return ""


def _build_auth_provider():
    """Return a FastMCP auth provider based on MCP_AUTH."""
    if AUTH_MODE == "none":
        return None

    if AUTH_MODE == "api_key":
        from fastmcp.server.auth import StaticTokenVerifier

        key = os.environ.get("MCP_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "MCP_AUTH=api_key requires MCP_API_KEY to be set "
                "(the bearer token clients must send)."
            )
        return StaticTokenVerifier(
            {key: {"client_id": "api-key", "scopes": []}},
        )

    if AUTH_MODE == "oauth":
        from fastmcp.server.auth import OAuthProvider

        if not PUBLIC_URL:
            raise RuntimeError(
                "MCP_AUTH=oauth requires PUBLIC_URL (e.g. "
                "https://your-app.example.com) so OAuth discovery works."
            )
        # Dynamic client registration (RFC 7591) is what claude.ai and Gemini
        # use to obtain a client_id before the authorization-code flow.
        return OAuthProvider(
            base_url=PUBLIC_URL,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )

    raise RuntimeError(
        f"Unsupported MCP_AUTH={AUTH_MODE!r}. Use one of: none, api_key, oauth."
    )


# ---------------------------------------------------------------------------
# FastMCP server instance
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "OWID Data Tools",
    instructions=_INSTRUCTIONS,
    auth=_build_auth_provider(),
)


# ---------------------------------------------------------------------------
# Health check (unauthenticated, useful for deployment probes)
# ---------------------------------------------------------------------------
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse(
        {
            "status": "ok",
            "auth": AUTH_MODE,
            "mcp_endpoint": f"{_public_base(request)}{MCP_PATH}",
            "rest_base": f"{_public_base(request)}/api",
        }
    )


# ---------------------------------------------------------------------------
# REST API Routes (public OWID data; used by client-side chart scaffolds)
# ---------------------------------------------------------------------------


@mcp.custom_route("/api/data/{slug}", methods=["GET"])
async def rest_get_data(request):
    from urllib.parse import unquote

    slug = request.path_params.get("slug", "")
    query = dict(request.query_params)

    columns = None
    if "columns" in query:
        columns = [c.strip() for c in unquote(query["columns"]).split(",") if c.strip()]

    year = int(query["year"]) if "year" in query else None
    year_start = int(query["year_start"]) if "year_start" in query else None
    year_end = int(query["year_end"]) if "year_end" in query else None
    country = query.get("country")
    limit = int(query.get("limit", "2000"))

    try:
        df = _cached_fetch_df(slug).copy()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=404)

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

    if columns:
        needed = list(
            dict.fromkeys(
                ([country_col] if country_col and country_col not in columns else [])
                + ([year_col] if year_col and year_col not in columns else [])
                + columns
            )
        )
        missing = [c for c in columns if c not in df.columns]
        if missing:
            return JSONResponse(
                {
                    "error": f"Columns not found: {missing}",
                    "available": list(df.columns),
                },
                status_code=400,
            )
        df = df[[c for c in needed if c in df.columns]]

    df = df.dropna(how="all")
    return JSONResponse(df.head(limit).to_dict(orient="records"))


@mcp.custom_route("/api/schema/{slug}", methods=["GET"])
async def rest_get_schema(request):
    slug = request.path_params.get("slug", "")

    try:
        df = _cached_fetch_df(slug)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=404)

    country_col, year_col = _detect_cols(df)
    structural = {
        str(country_col).lower() if country_col else "",
        str(year_col).lower(),
        "code",
        "entity_code",
        "country_code",
    }
    value_cols = [
        c
        for c in df.columns
        if str(c).lower() not in structural and pd.api.types.is_numeric_dtype(df[c])
    ]

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

    return JSONResponse(
        {
            "slug": slug,
            "total_rows": len(df),
            "country_col": country_col,
            "year_col": year_col,
            "value_cols": value_cols,
            "columns": schema,
            "rest_endpoint": f"{_public_base(request)}/api/data/{slug}",
        }
    )


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
# MCP Tools
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
    Use instead of a plain-text table when generating visualizations or widgets.
    Keep limit low (<=100) to avoid blowing up context. For full datasets, use
    the REST endpoint (see get_dataset_schema) in a fetch() call instead.

    NOTE: For rendering charts inside sandboxed clients, prefer generate_chart_html()
    which fetches + inlines data server-side. This tool exists as a fallback for
    small datasets or when you need raw numbers in context.

    Args:
        slug: EXACT slug returned by search_owid.
        country: Optional country filter.
        year_start: Optional start year.
        year_end: Optional end year.
        columns: Specific columns to include.
        limit: Max rows to return (default 100).
    """
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
            return json.dumps(
                {
                    "error": f"Columns not found: {missing}",
                    "available": list(df.columns),
                }
            )
        keep = list(
            dict.fromkeys(
                ([country_col] if country_col and country_col not in columns else [])
                + ([year_col] if year_col and year_col not in columns else [])
                + list(columns)
            )
        )
        df = df[keep]

    df = df.dropna(how="all")
    return json.dumps(df.head(limit).to_dict(orient="records"), default=str)


@mcp.tool
async def get_dataset_schema(slug: str) -> str:
    """
    Returns structural metadata for an OWID dataset WITHOUT fetching row data.
    This is the FIRST tool to call after search_owid when building a chart.

    Returns:
        - slug: Dataset identifier
        - country_col: Column name for country/entity (for grouping & filtering)
        - year_col: Column name for year/time (for x-axis)
        - value_cols: List of numeric columns suitable for charting (pick one for y-axis)
        - columns: Full schema with dtypes, min/max/nulls for numeric, samples for strings
        - rest_endpoint: URL to fetch actual data via client-side fetch()
        - total_rows: Dataset size

    Usage: Read the schema, pick the right columns, then either:
        1. Use generate_chart_scaffold() to get an HTML skeleton, fill in chart config
        2. Call fetch(rest_endpoint) from your own code with query params:
           ?country=X&year_start=2000&year_end=2023&columns=col1,col2

    Args:
        slug: EXACT slug returned by search_owid.
    """
    try:
        _check_owid_catalog()
    except RuntimeError as e:
        return str(e)

    try:
        df = _cached_fetch_df(slug)
    except Exception as e:
        return json.dumps({"error": str(e)})

    country_col, year_col = _detect_cols(df)
    structural = {
        str(country_col).lower() if country_col else "",
        str(year_col).lower(),
        "code",
        "entity_code",
        "country_code",
    }
    value_cols = [
        c
        for c in df.columns
        if str(c).lower() not in structural and pd.api.types.is_numeric_dtype(df[c])
    ]

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

    return json.dumps(
        {
            "slug": slug,
            "total_rows": len(df),
            "country_col": country_col,
            "year_col": year_col,
            "value_cols": value_cols,
            "columns": schema,
            "rest_endpoint": f"{_public_base()}/api/data/{slug}",
        },
        indent=2,
    )


@mcp.tool
async def generate_chart_scaffold(
    chart_type: str = "line",
    value_column: Optional[str] = None,
    country: Optional[str] = None,
    year_start: Optional[int] = None,
    year_end: Optional[int] = None,
    title: Optional[str] = None,
    x_label: Optional[str] = None,
    y_label: Optional[str] = None,
    height: int = 460,
) -> str:
    """
    Returns a complete, self-contained HTML scaffold for client-side chart rendering.

    Provides boilerplate: Plotly CDN, theme, fetch() template, and empty chart config.
    The LLM fills in the data parsing logic based on get_dataset_schema().

    NOTE: Intended for use OUTSIDE sandboxed clients (e.g. developer environments
    where CSP is not restricted). Inside sandboxed clients, use generate_chart_html()
    instead — it fetches and inlines all data server-side, bypassing the iframe CSP.

    ALL arguments are simple strings or ints — do NOT pass objects or dicts.

    Args:
        chart_type: One of: line, bar, area, scatter. Default is line.
        value_column: Exact column name from the schema to plot on y-axis. Must be a plain string, e.g. "life_expectancy_0".
        country: Single country name to filter, e.g. "United States". Leave empty for all.
        year_start: Start year as integer, e.g. 2000.
        year_end: End year as integer, e.g. 2023.
        title: Chart title as plain string, e.g. "CO2 Emissions".
        x_label: X-axis label as plain string, e.g. "Year".
        y_label: Y-axis label as plain string, e.g. "Emissions (tonnes)".
        height: Chart height in pixels. Default 460.
    """
    from urllib.parse import urlencode

    query_hints = {}
    if country:
        query_hints["country"] = country
    if year_start is not None:
        query_hints["year_start"] = str(year_start)
    if year_end is not None:
        query_hints["year_end"] = str(year_end)
    if value_column:
        query_hints["columns"] = value_column
    query_string = urlencode(query_hints) if query_hints else ""

    trace_type_map = {
        "line": '{"mode": "lines+markers"}',
        "bar": '{"type": "bar"}',
        "area": '{"mode": "lines", "fill": "tozeroy"}',
        "scatter": '{"mode": "markers"}',
    }
    trace_extra = trace_type_map.get(chart_type.lower(), trace_type_map["line"])

    import html as html_mod

    scaffold = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{html_mod.escape(title or "OWID Chart")}</title>
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
  var COLORS = {json.dumps(_SERIES_COLORS)};
  var BG = "{_BG}", TEXT = "{_TEXT}", MUTED = "{_MUTED}";
  var GRID = "{_GRID}", BORDER = "{_BORDER}";
  var FONT = "{_FONT}";

  var TITLE = {json.dumps(title or "")};
  var X_LABEL = {json.dumps(x_label or "Year")};
  var Y_LABEL = {json.dumps(y_label or "")};
  var TRACE_CONFIG = {trace_extra};

  // Replace the URL below with the actual rest_endpoint from get_dataset_schema
  var ENDPOINT = "{_public_base()}/api/data/{{slug}}";  /* <-- REPLACE with actual slug */
  var QUERY = "{query_string}";  /* <-- Add query params as needed */
  var URL = ENDPOINT + (QUERY ? "?" + QUERY : "");

  var layout = {{
    title: {{text: TITLE, font: {{size: 18, color: TEXT, family: "'Playfair Display', Georgia, serif"}}, x: 0.04}},
    paper_bgcolor: BG, plot_bgcolor: BG,
    font: {{color: TEXT, family: FONT}},
    margin: {{l: 72, r: 24, t: 60, b: 52}},
    legend: {{bgcolor: "rgba(255,255,255,0.8)", bordercolor: BORDER, borderwidth: 1, font: {{color: TEXT, size: 12}}}},
    xaxis: {{gridcolor: GRID, linecolor: TEXT, tickcolor: TEXT, tickformat: "d", title: {{text: X_LABEL, font: {{size: 13, color: MUTED}}}}, tickfont: {{color: MUTED}}}},
    yaxis: {{gridcolor: GRID, linecolor: TEXT, tickcolor: TEXT, title: {{text: Y_LABEL, font: {{size: 13, color: MUTED}}}}, tickfont: {{color: MUTED}}, zeroline: true, zerolinecolor: GRID, tickformat: "~s"}},
    hovermode: "x unified",
    hoverlabel: {{bgcolor: BG, bordercolor: BORDER, font: {{color: TEXT, size: 13, family: FONT}}}},
  }};

  var config = {{responsive: true, displayModeBar: true, displaylogo: false}};

  // ── TODO: Replace this with actual data parsing ──
  // The data is a JSON array of objects. Example structure:
  //   [{{"year": 2000, "country": "France", "value": 75.2}}, ...]
  // Use Array.prototype.reduce() to group by country, then build traces.

  var traces = [];  /* <-- Build traces from fetched data here */

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

    instructions = (
        f"<!-- CHART SCAFFOLD — Fill in the TODO sections to complete the chart.\n"
        f"     Chart type: {chart_type}\n"
        f"     Fetch URL pattern: {_public_base()}/api/data/{{slug}}?{query_string}\n"
        f"     Use get_dataset_schema() to discover exact column names.\n"
        f"-->\n"
    )
    return instructions + scaffold


def _owid_grapher_url(
    slug: str,
    tab: Optional[str] = None,
    time: Optional[str] = None,
    countries: Optional[List[str]] = None,
    hide_controls: bool = False,
) -> str:
    """Build the official OWID grapher URL (same format as OWID's own
    share/embed dialog: canonical URL + tab/time/country/hideControls)."""
    params: List[tuple] = []
    if tab:
        params.append(("tab", tab))
    if hide_controls:
        params.append(("hideControls", "true"))
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
        params.append(("country", urllib.parse.quote(country_str, safe="()~")))
        if tab == "map":
            # The map view highlights countries via mapSelect
            params.append(
                ("mapSelect", urllib.parse.quote("~" + country_str, safe="()~"))
            )
    if time:
        params.append(("time", urllib.parse.quote(time, safe=".~")))

    query = "&".join(f"{k}={v}" for k, v in params)
    return f"https://ourworldindata.org/grapher/{slug}" + (f"?{query}" if query else "")


def _build_owid_embed_html(url: str, slug: str, height: int) -> str:
    """HTML page embedding the official OWID grapher in an iframe.

    Uses OWID's own embed iframe attributes (width 100%, border none,
    web-share allow-list). If the host sandbox blocks external iframes
    (load never fires), a fallback link is shown instead.
    """
    height = max(int(height), 600)
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{html.escape(slug)}</title>
<style>
*,*::before,*::after{{box-sizing:border-box}}
html,body{{margin:0;padding:0;width:100%;background:#ffffff;color:#4e4e4e;font-family:Lato,'Helvetica Neue',Helvetica,Arial,sans-serif;}}
</style>
</head><body>
<iframe id="owid-frame" src="{url}" loading="lazy"
  style="width:100%;height:{height}px;border:0px none;display:block;"
  allow="web-share; clipboard-write" title="Our World in Data chart"></iframe>
<div id="owid-fallback" style="display:none;padding:32px 16px;text-align:center;font-size:14px;color:#5b5b5b;">
  <p style="margin:0 0 8px;">This environment blocks embedded OWID charts.</p>
  <p style="margin:0;"><a href="{url}" target="_blank" rel="noopener" style="color:#002147;font-weight:bold;">Open the official Our World in Data chart &#8599;</a></p>
</div>
<div style="text-align:center;margin:8px 0 4px;font-size:12px;color:#767676;">
  <a href="{url}" target="_blank" rel="noopener" style="color:#767676;text-decoration:none;">View chart on Our World in Data &#8599;</a>
  &nbsp;&middot;&nbsp; Data: Our World in Data (CC BY)
</div>
<script>
(function(){{
  var frame = document.getElementById('owid-frame');
  var loaded = false;
  frame.addEventListener('load', function(){{ loaded = true; }});
  setTimeout(function(){{
    if (!loaded) {{
      frame.style.display = 'none';
      document.getElementById('owid-fallback').style.display = 'block';
    }}
  }}, 5000);
  function syncHeight(){{
    var h = document.documentElement.scrollHeight;
    try{{ if(window.frameElement) window.frameElement.style.height = h + 'px'; }}catch(e){{}}
  }}
  window.addEventListener('load', syncHeight);
  if(typeof ResizeObserver !== 'undefined') new ResizeObserver(syncHeight).observe(document.body);
  [300, 900].forEach(function(t){{ setTimeout(syncHeight, t); }});
}})();
</script></body></html>"""


@mcp.tool
async def generate_chart_html(
    slug: str,
    embed: bool = True,
    tab: Optional[str] = None,
    time: Optional[str] = None,
    countries: Optional[List[str]] = None,
    hide_controls: bool = False,
    value_column: Optional[str] = None,
    chart_type: str = "line",
    log_scale: bool = False,
    year_start: Optional[int] = None,
    year_end: Optional[int] = None,
    title: Optional[str] = None,
    x_label: Optional[str] = None,
    y_label: Optional[str] = None,
    height: int = 600,
) -> str:
    """
    Returns fully self-contained HTML for an OWID chart.

    By default (embed=True) this embeds the REAL interactive Our World in Data
    grapher in an iframe — official OWID styling, log/linear toggle, source
    notes, event annotations, and share/download controls — instead of
    rebuilding a worse copy from raw JSON.

    Only use embed=False when the target environment blocks external iframes
    (e.g. claude.ai artifact sandboxes): it then rebuilds the chart client-side
    with the official OWID color palette, Lato/Playfair typography, an optional
    log/linear toggle, and a source attribution footer.

    Args:
        slug: EXACT slug returned by search_owid. Required.
        embed: True (default) -> official OWID grapher iframe.
               False -> custom Plotly reconstruction from raw data.
        tab: Optional OWID view: 'chart' (default), 'map', or 'table'.
        time: Optional time range, e.g. '1950..latest' or '2023'.
        countries: Optional list of entity names to pre-select (exact names
                   from search_owid). E.g. ['United States', 'China'] ->
                   country=USA~CHN in the grapher URL.
        hide_controls: Hide the grapher's external controls in the embed.
        value_column: (embed=False only) column from the schema to plot.
        chart_type: (embed=False only) line | bar | area | scatter.
        log_scale: (embed=False only) start on log scale and add a
                   Linear/Log toggle. Avoid if data contains zero/negative
                   values.
        year_start / year_end: (embed=False only) integer years.
        title / x_label / y_label: (embed=False only) chart labels.
        height: iframe/chart height in pixels (min 600 for the embed).
    """
    if embed:
        url = _owid_grapher_url(
            slug,
            tab=tab,
            time=time,
            countries=countries,
            hide_controls=hide_controls,
        )
        return _build_owid_embed_html(url, slug, height)

    # ── Fallback: custom Plotly reconstruction with official OWID styling ──
    try:
        _check_owid_catalog()
    except RuntimeError as e:
        return str(e)

    try:
        df = _cached_fetch_df(slug).copy()
    except Exception as e:
        return f"Error fetching '{slug}': {e}"

    country_col, year_col = _detect_cols(df)
    structural = {
        str(country_col).lower() if country_col else "",
        str(year_col).lower(),
        "code",
        "entity_code",
        "country_code",
    }

    val_col = _detect_value_col(df, structural, value_column)
    if val_col is None:
        return f"Could not detect a numeric value column in '{slug}'."

    if countries and country_col:
        available = df[country_col].dropna().unique().tolist()
        entity_list = [_fuzzy_match_country(c, available) for c in countries[:8]]
    elif country_col:
        all_entities = df[country_col].dropna().unique().tolist()
        df_latest = df.dropna(subset=[year_col, val_col]).copy()
        df_latest[year_col] = pd.to_numeric(df_latest[year_col], errors="coerce")
        df_latest = df_latest.dropna(subset=[year_col])
        df_latest[year_col] = df_latest[year_col].astype(int)
        if not df_latest.empty:
            max_year = int(df_latest[year_col].max())
            latest = df_latest[df_latest[year_col] >= max_year - 1].dropna(
                subset=[val_col]
            )
            entity_means = (
                latest.groupby(country_col)[val_col]
                .mean()
                .sort_values(ascending=False)
                .head(8)
            )
            entity_list = entity_means.index.tolist()
        else:
            entity_list = [str(e) for e in all_entities[:8]]
    else:
        entity_list = [None]

    traces = []
    for idx, entity in enumerate(entity_list):
        entity_df = (
            df[[country_col, year_col, val_col]].copy()
            if country_col
            else df[[year_col, val_col]].copy()
        )

        if entity and country_col:
            entity_df = entity_df[entity_df[country_col].astype(str) == entity]

        entity_df[year_col] = pd.to_numeric(entity_df[year_col], errors="coerce")
        entity_df = entity_df.dropna(subset=[year_col])
        entity_df[year_col] = entity_df[year_col].astype(int)

        if year_start is not None:
            entity_df = entity_df[entity_df[year_col] >= int(year_start)]
        if year_end is not None:
            entity_df = entity_df[entity_df[year_col] <= int(year_end)]

        entity_df[val_col] = pd.to_numeric(entity_df[val_col], errors="coerce")
        entity_df = entity_df.dropna(subset=[val_col])

        entity_df = entity_df.groupby(year_col, as_index=False)[val_col].mean()
        entity_df = entity_df.sort_values(year_col).reset_index(drop=True)

        if entity_df.empty:
            continue

        color = _SERIES_COLORS[idx % len(_SERIES_COLORS)]
        entity_name = entity if entity else val_col
        trace = _make_trace(
            x=entity_df[year_col].tolist(),
            y=[float(v) for v in entity_df[val_col].tolist()],
            name=str(entity_name),
            color=color,
            chart_type=chart_type,
        )
        traces.append(trace)

    if not traces:
        return f"No chartable data found for '{slug}'."

    chart_title = title or f"{slug.replace('-', ' ').title()}"
    y_axis_label = y_label or val_col.replace("_", " ").title()

    return _build_html(
        title=chart_title,
        traces=traces,
        x_label=x_label or "Year",
        y_label=y_axis_label,
        height=height,
        log_scale=log_scale,
        source_url=f"https://ourworldindata.org/grapher/{slug}",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def _main():
    await mcp.run_http_async(
        transport="streamable-http",
        host=HOST,
        port=PORT,
        path=MCP_PATH,
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["*"],
                allow_headers=["*"],
            )
        ],
    )


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
