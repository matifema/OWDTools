# OWID Data Tools for Open WebUI

An [Open WebUI](https://github.com/open-webui/open-webui) tool plugin **and a remote MCP server** that bring the wealth of data from **[Our World in Data (OWID)](https://ourworldindata.org/)** directly into your LLM chats.

This tool enables your AI assistant to search the OWID catalog, seamlessly embed official interactive charts, and retrieve raw tabular data for deep analysis—all without leaving the chat interface. It works as an Open WebUI tool and as an MCP server you can connect directly from **Gemini** and **Claude**.

## Screenshots

| OWID Official Maps | OWID Official Charts |
| :---: | :---: |
| <img src="official_map.png" width="400"/> | <img src="official_bubble.png" width="400"/> |
| **Custom Charts** | **More Custom Charts** |
| <img src="custom_line.png" width="400"/> | <img src="custom_area.png" width="400"/> |

## Features

- **Semantic Catalog Search:** The LLM can quickly search through hundreds of OWID datasets (e.g., life expectancy, CO2 emissions, poverty) and find the exact metrics needed using fuzzy-matching and intelligent caching.
- **Official Interactive Embeds:** Automatically embeds the authentic, fully interactive OWID Grapher directly into the chat interface via responsive iframes. Enjoy maps, multi-country comparisons, and rich tooltips just as they appear on the official website.
- **Raw Data Extraction:** When numerical analysis is required, the tool can extract specific columns, filter by country and year, and return raw data in clean Markdown tables for the LLM to analyze.
- **Dark-Mode Native:** Designed to integrate flawlessly with Open WebUI's aesthetic.


## Installation

1. Open your Open WebUI interface.
2. Navigate to **Workspace** -> **Tools**.
3. Click the **+** button to import a new tool.
4. Copy the entire contents of `owid_tools.py` and paste it into the code editor.
5. Save and enable the tool for your LLM models.

**Prerequisites:** 
Ensure the Python environment running Open WebUI has the following packages installed:
```bash
pip install pandas owid-catalog
```

**Important Note for Pip Installations:**
If you installed Open WebUI via `pip` (rather than Docker), you may need to run `npx inject` in your environment to properly enable frontend features like interactive iframes.

## Using from Gemini & Claude (remote MCP)

The repository also ships `mcp_server.py`, a standalone MCP server that Gemini and Claude can connect to directly from their websites.

### 1. Install & run

```bash
pip install -r requirements.txt
python mcp_server.py
```

By default the server listens on `http://0.0.0.0:8000` and exposes the MCP endpoint at `/mcp`.

### 2. Expose it over HTTPS

Gemini and Claude can only reach your server through a public HTTPS URL. Pick whichever fits:

- **Cloudflare Containers** (recommended — full deployment on Cloudflare, see next section)
- **Cloudflare Quick Tunnel** (zero setup, no domain):
  ```bash
  # from a second terminal — cloudflared gives you a temporary public URL
  cloudflared tunnel --url http://localhost:8000
  ```
  Use the printed `https://<random>.trycloudflare.com` URL as your base URL.
- **Tailscale Funnel** (no public IP needed):
  ```bash
  tailscale funnel 8000
  ```
- Any reverse proxy / cloud host (Fly.io, Render, a VPS with Caddy, …).

## Deploy on Cloudflare (Workers + Containers)

Everything needed is already in the repo: `Dockerfile`, `worker/index.js`,
`wrangler.jsonc`, `package.json`. One command ships the Python MCP server as a
Cloudflare Container behind a Worker — no server to manage.

```bash
npm install            # installs wrangler + @cloudflare/containers
npx wrangler login     # authenticate with your Cloudflare account
npx wrangler deploy    # builds the Docker image and deploys Worker + Container
```

Requirements: Docker running locally, and a Cloudflare **Workers Paid plan**
(Containers is not on the free tier). After deploy, your MCP endpoint is:

```text
https://owd-tools.<your-workers-subdomain>.workers.dev/mcp
```

The first request after a deploy takes a minute or two while Cloudflare
provisions the container (it sleeps after 30 minutes idle). Check status with
`npx wrangler containers list` and logs with `npx wrangler containers logs`.

No environment variables are required: OWID data is public, and the server
auto-detects its public URL from the request. Optionally set `vars` in
`wrangler.jsonc` (e.g. `PUBLIC_URL`, `MCP_AUTH`) or secrets with
`npx wrangler secret put MCP_API_KEY` if you want to gate access.

### 3. Authentication (optional)

**You do not need any credentials to fetch OWID data — everything is public.**
Authentication here only controls who may call *your* server (protects your compute
from strangers if the URL is public). It is disabled by default.

Set the `MCP_AUTH` environment variable only if you want to gate access
(see `.env.example`):

| Mode | When to use |
| :--- | :--- |
| `none` (default) | Public or private deployments; Gemini and Claude connect with no token |
| `api_key` | Gemini's **API key** auth — set `MCP_API_KEY` and use it as the bearer token |
| `oauth` | Standard OAuth 2.1 sign-in (e.g. if you want claude.ai's connector to authenticate users before using your server) — set `PUBLIC_URL` |

### 4. Connect

**Gemini** — In [Google AI Studio](https://aistudio.google.com/) or the Gemini app, go to **Tools → MCP**, choose **Add server**, and enter:

```text
https://<your-public-url>/mcp
```

Choose **No authentication** (or API key / OAuth if you enabled them).

**Claude** — In [claude.ai](https://claude.ai), open **Settings → Connectors → Add custom connector** and enter the same URL. If the server advertises OAuth (`MCP_AUTH=oauth`), Claude will run the authorization flow; otherwise it connects with no authentication.

After connecting, you can ask either assistant to e.g. *"search OWID for life expectancy and chart the United States"* — it will call the exposed tools (`search_owid`, `generate_chart_html`, `get_dataset_schema`, `get_owid_data_json`, …).

> `generate_chart_html` embeds the **real** interactive OWID grapher (official styling, log/linear toggle, source notes, share/download controls) by default. It only falls back to a custom Plotly reconstruction — with OWID's own color palette, Lato/Playfair fonts, log toggle, and CC BY attribution — when you pass `embed=false` (e.g. for sandboxes that block external iframes).

## Configuration (Valves)

You can configure the tool's behavior via the Valves settings in Open WebUI:

- `allow_iframe_embedding` (Default: `True`): Allows embedding of the official OWID interactive iframes. If set to `False`, the tool will return direct hyperlinks instead.
- `chart_height_px` (Default: `460`): The default height for embedded charts.
- `max_table_rows` (Default: `20`): Maximum number of rows returned when fetching raw tabular data, protecting the LLM's context window.
- `max_search_results` (Default: `5`): Limits the number of search results returned per query to balance relevance and token usage.

## Disclaimer & Data Ownership

**This project is not affiliated with, endorsed by, or sponsored by Our World in Data.**

This tool is simply an open-source wrapper designed to facilitate access to public data. 

All data, charts, and associated metadata retrieved by this tool are the property of **[Our World in Data](https://ourworldindata.org/)** and their respective authors and data providers. OWID publishes their research and data as open access under the Creative Commons BY license (CC BY 4.0), though specific underlying datasets may have different licenses. 

Please refer to the [OWID Terms of Use](https://ourworldindata.org/how-to-use-our-world-in-data) and ensure you properly cite the original sources when utilizing this data in your own projects or publications.

## License

The wrapper code in this repository is open-sourced under the MIT License. See the [LICENSE](LICENSE) file for details.