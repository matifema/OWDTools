import { Container, getContainer } from "@cloudflare/containers";

/**
 * OWID Data Tools — Cloudflare Containers proxy.
 *
 * Every request is forwarded as-is to the Python MCP server running inside
 * the container (port 8000). No auth token is needed for OWID data itself
 * (it's all public); env vars below only optionally gate access to the server.
 *
 * Configuration comes from `vars` / secrets in wrangler.jsonc (available on
 * `env`) and is injected into the container on every start.
 */
export class OWDToolsContainer extends Container {
  defaultPort = 8000;
  sleepAfter = "30m"; // container sleeps when idle to save resources
  enableInternet = true; // needed to download public OWID catalog data
  pingEndpoint = "localhost/health";

  constructor(ctx, env) {
    super(ctx, env);
    this.envVars = {
      // All optional — the server works with no auth and auto-detects its URL.
      PUBLIC_URL: env.PUBLIC_URL || "",
      MCP_AUTH: env.MCP_AUTH || "none", // none | api_key | oauth
      MCP_API_KEY: env.MCP_API_KEY || "",
      MAX_TABLE_ROWS: String(env.MAX_TABLE_ROWS ?? 20),
      MAX_SEARCH_RESULTS: String(env.MAX_SEARCH_RESULTS ?? 5),
    };
  }
}

export default {
  async fetch(request, env) {
    // One shared instance; Cloudflare scales it up to max_instances if needed.
    const container = getContainer(env.OWD_TOOLS_CONTAINER, "default");
    return container.fetch(request);
  },
};
