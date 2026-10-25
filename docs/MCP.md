# Built-in MCP server

The app ships an [MCP](https://modelcontextprotocol.io) server, so Claude Desktop, Cursor,
VS Code and other MCP clients can run the whole GEO loop in a conversation:

**connect store → audit → track AI answers → draft fixes → approve/apply → verify**

Two transports, one implementation:

| Transport | Endpoint | Best for |
|---|---|---|
| stdio | `python -m app.mcp_server` | Claude Desktop, Cursor, local IDE agents |
| Streamable HTTP | `http://127.0.0.1:8000/mcp` | Remote-capable clients, shared local app |

Mock mode works with zero setup: the sample catalog is already there.

## Quick start (stdio)

Use the absolute path to the same Python environment where the app is installed, and set
`PYTHONPATH` to the repo root so `-m app.mcp_server` resolves from any working directory.

### Claude Desktop

Edit `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`;
Windows: `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "shopify-geo": {
      "command": "C:\\path\\to\\shopify-geo-agent\\.venv\\Scripts\\python.exe",
      "args": ["-m", "app.mcp_server"],
      "env": {
        "PYTHONPATH": "C:\\path\\to\\shopify-geo-agent"
      }
    }
  }
}
```

macOS/Linux:

```json
{
  "mcpServers": {
    "shopify-geo": {
      "command": "/path/to/shopify-geo-agent/.venv/bin/python",
      "args": ["-m", "app.mcp_server"],
      "env": {"PYTHONPATH": "/path/to/shopify-geo-agent"}
    }
  }
}
```

Restart Claude Desktop. The tools appear under the MCP/server menu.

### Cursor

Create `.cursor/mcp.json` in the project (or use Settings → MCP):

```json
{
  "mcpServers": {
    "shopify-geo": {
      "command": "C:\\path\\to\\shopify-geo-agent\\.venv\\Scripts\\python.exe",
      "args": ["-m", "app.mcp_server"],
      "env": {"PYTHONPATH": "C:\\path\\to\\shopify-geo-agent"}
    }
  }
}
```

### VS Code (Copilot agent mode)

`.vscode/mcp.json`:

```json
{
  "servers": {
    "shopify-geo": {
      "type": "stdio",
      "command": "C:\\path\\to\\shopify-geo-agent\\.venv\\Scripts\\python.exe",
      "args": ["-m", "app.mcp_server"],
      "env": {"PYTHONPATH": "C:\\path\\to\\shopify-geo-agent"}
    }
  }
}
```

## Quick start (HTTP)

Start the web app normally (`python run.py`). The MCP endpoint is mounted at `/mcp` on the
same server:

```json
{
  "mcpServers": {
    "shopify-geo": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

For clients that only speak stdio but support remote URLs, use your client's HTTP/SSE bridge.

### Remote access

The HTTP endpoint is **loopback-only by default**. Requests from another machine must send a
bearer token:

1. Set `MCP_TOKEN` in the environment that runs the web app (or save an `mcp_token` in
   Settings). Use a long random value.
2. Send `Authorization: Bearer <token>` on every MCP request.

```powershell
$env:MCP_TOKEN = "paste-a-long-random-token"
python run.py
```

The guard allows loopback clients whose `Host` is also loopback. A browser `Origin` that is
not loopback is rejected even from `127.0.0.1` (DNS-rebinding defense). Non-loopback requests
without a valid token get `403`.

The token is compared in constant time and is never logged or returned by a tool.

## Connecting a real Shopify store

No MCP server can remove Shopify's authorization requirement for writes. A store must grant
an Admin API token before anything can be read or changed. The server makes that one
conversation:

1. Ask the client to call `shopify_setup_guide`.
2. In Shopify admin: **Settings → Apps and sales channels → Develop apps → Create an app**.
3. **Configuration → Admin API integration**: enable `read_products` and `write_products`.
4. **Install app**, then reveal the **Admin API access token** (`shpat_...`).
5. Call:

```text
connect_shopify(
  shop_domain="your-store.myshopify.com",
  admin_api_token="shpat_...",
  sync=true
)
```

The tool validates the domain/token against Shopify, stores them in `config.json` on this
machine, switches to live mode, and syncs the catalog. It never echoes the token back.
`disconnect_shopify` erases the local copy and returns to mock mode; to revoke the grant
itself, delete the custom app in Shopify admin.

> The Admin API token is a write credential. Treat it like a password: keep `config.json` out
> of git, don't paste it into prompts you don't control, and revoke it in Shopify when done.

## Tools

| Tool | Type | What it does |
|---|---|---|
| `store_status` | read | Mode, connection, catalog/prompt/fix counts, last sync |
| `shopify_setup_guide` | read | Custom-app steps, scopes, where the token goes |
| `connect_shopify` | write | Validate + save domain/token, switch to live, optional first sync |
| `disconnect_shopify` | write | Erase local token, return to mock mode |
| `sync_catalog` | write | Pull live catalog into the local mirror |
| `list_products` | read | Products with GEO score/grade/issue count, worst first |
| `get_product` | read | Full product + scoring + readiness checks |
| `product_readiness` | read | Taxonomy/GTIN/SEO/variant/price/JSON-LD checks |
| `list_prompts` / `add_prompt` | read/write | Manage tracked shopping prompts |
| `run_tracking` | write | Run prompts against configured official provider APIs |
| `visibility_summary` | read | Mention rate, Wilson CIs, per-prompt stats, sources, competitors, trends |
| `generate_fixes` | write (queue only) | Draft title/FAQ/description fixes; nothing goes live |
| `list_fixes` | read | Pending/applied/discarded fixes with before/after previews |
| `apply_fix` | **write to Shopify** | Apply one approved fix; refuses stale drafts |
| `discard_fix` | write | Drop a queued fix without touching the store |

Writes are explicit tool calls. `generate_fixes` only queues drafts; `apply_fix` is the
approval gate and applies exactly one fix id at a time. It re-checks that the live field
still matches the draft (staleness check) and refuses to overwrite a merchant's newer edit.

## Suggested conversation

```text
You: Use shopify-geo. What's my store status?
You: Connect my store — guide me through the Shopify token setup.
You: Audit the catalog and show me the five worst products.
You: Generate fixes, but don't apply anything yet. Show me the title diffs.
You: Apply fix 12. Then show me the updated product.
You: Which prompts are we tracking? Add "best waterproof backpack for travel".
You: Run tracking for the prompts we have keys for, then summarize visibility.
```

The model is instructed to report missing product facts as gaps rather than inventing them,
and to only extend/reorder titles (never remove the product name).

## Troubleshooting

- **`ModuleNotFoundError: No module named 'app'`** — set `PYTHONPATH` to the repo root, as
  shown above, or launch from the repo directory.
- **`ModuleNotFoundError: No module named 'mcp'`** — install the project dependencies
  (`pip install -e .` or `pip install -r requirements.txt`); the server uses MCP SDK v2.
- **HTTP `403`** — the request is not loopback, or the bearer token is missing/wrong.
- **HTTP `503`** — the MCP endpoint was called before the web app's lifespan started; start
  the app with `python run.py` or `uvicorn app.main:app`.
- **`connect_shopify` says HTTP 401/403** — the token is not from this store's custom app, or
  `read_products`/`write_products` were not granted before installing.
- **Empty catalog after connecting** — call `sync_catalog`; the first sync may have failed if
  the token lacks `read_products`.
- **Tools see different data than the web UI** — both use `geo_agent.db` and `config.json` in
  the repo root. Run the stdio server from the same checkout/environment.

## Security notes

- Binds to `127.0.0.1` by default; non-loopback HTTP requires `MCP_TOKEN`.
- Secrets live in `config.json` (gitignored) or environment variables; never in HTML, logs or
  tool output.
- The HTTP guard blocks non-loopback `Origin` even from a loopback address.
- Only official AI provider APIs are used for answers. Paste mode remains available in the web
  UI for engines without an API.
- `apply_fix` is approval-gated and staleness-checked. Rollback/snapshots are on the roadmap
  (Phase 6) and are not yet claimed by these tools.
