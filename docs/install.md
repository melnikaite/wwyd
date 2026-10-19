# Installing wwyd

wwyd is one small server. Pick **one** way to run it:

- **A. On your own computer** — easiest; only you (and apps on this computer) can reach it.
- **B. With Docker on your computer** — same as A, inside a container.
- **C. On a cloud server with your own domain** — needed for claude.ai and ChatGPT, which
  connect from the internet.

Then **D** connects your AI apps, **E** covers updates and backups, **F** troubleshooting.

All your data lives in one folder (the *data folder*): `~/.wwyd` on your computer, or the
`wwyd-data` Docker volume in a container.

---

## A. On your own computer (uv)

1. **Install uv** (a small tool that downloads and runs Python programs).
   - macOS / Linux: open Terminal and run
     `curl -LsSf https://astral.sh/uv/install.sh | sh`
   - Windows: open PowerShell and run
     `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

   Close the terminal window and open a new one.

   *What you should see:* `uv --version` prints a version number.
   *If you don't:* see the uv installation guide, https://docs.astral.sh/uv/getting-started/installation/

2. **Start wwyd:** run `uvx wwyd`

   > To run the latest source code instead of the PyPI release:
   > `uvx --from git+https://github.com/melnikaite/wwyd wwyd`

   *What you should see:* a line `wwyd 0.2.0 listening on http://127.0.0.1:8740` and a line
   `Open this link to create the admin account: http://127.0.0.1:8740/setup?t=…`.
   *If you don't:* see [F](#f-troubleshooting).

3. **Open the setup link** in your browser and choose a login (your email) and a password.
   The first account is the administrator.

   *What you should see:* your account page, with connection instructions for Claude and
   ChatGPT.

4. Keep the terminal window open while you use wwyd. To stop it, press `Ctrl+C`. To start
   it again, run `uvx wwyd` again (no new setup link: your account already exists).

## B. With Docker on your computer

1. Install Docker Desktop: https://docs.docker.com/get-started/get-docker/
2. Run:

   ```sh
   docker run -d --name wwyd -v wwyd-data:/data -p 127.0.0.1:8740:8740 ghcr.io/melnikaite/wwyd:0.2.0
   ```

   Images are published for linux/amd64 and linux/arm64 with tags `X.Y.Z`, `X.Y` and
   `latest`. To build from source instead:
   `docker build -t wwyd:local https://github.com/melnikaite/wwyd.git#v0.2.0` and use
   `wwyd:local` as the image name.

3. Run `docker logs wwyd`.

   *What you should see:* `Open this link to create the admin account: http://127.0.0.1:8740/setup?t=…`
   *If you don't:* wait ten seconds and try again; see [F](#f-troubleshooting).

4. Open that link and create your account, as in A.3.

## C. On a cloud server with your own domain

You need: a domain name you control (e.g. `example.com`), and an account with a cloud
provider that offers Ubuntu virtual machines with "user data" / "cloud-init" (most do).
wwyd needs very little: 1 CPU and 1 GB RAM is enough.

1. **Create the virtual machine.** Choose Ubuntu 24.04 (or 26.04). In the "user data" /
   "cloud-init" field, paste the contents of
   [`deploy/cloud-init.yaml`](../deploy/cloud-init.yaml), and change
   `WWYD_DOMAIN=wwyd.example.com` to the name you want, e.g. `wwyd.example.com`.
   Add your SSH key so you can log in.
2. **Open the firewall** (in the provider's network or firewall settings): allow inbound
   TCP 22, 80 and 443, and UDP 443.
3. **Create the DNS record.** At the company where you manage your domain, add a record:
   type `A`, name `wwyd` (the part before your domain), value = the VM's public IPv4
   address. If the VM also has IPv6, add an `AAAA` record the same way.

   *What you should see:* after a few minutes, `ping wwyd.example.com` shows the VM's
   address.
4. **Wait about 5 minutes** for the VM to install everything. Then log in with
   `ssh <user>@wwyd.example.com` (the user name is shown by your provider, often `ubuntu`
   or `root`) and run `sudo wwyd-setup-link`.

   *What you should see:* `https://wwyd.example.com/setup?t=…`
   *If you don't:* see [F](#f-troubleshooting).
5. Open that link and create your account.

   *What you should see:* a padlock in the address bar and your account page.

The files on the VM live in `/opt/wwyd` (`docker-compose.yml`, `Caddyfile`, `.env`). The
same compose file works on any Linux server with Docker: copy `deploy/` there and run
`WWYD_DOMAIN=wwyd.example.com docker compose up -d`.

**Notes for hosted instances:**

- **Several users?** Set `WWYD_LLM_ALLOW_PRIVATE_BASE_URLS=false` (or
  `allow_private_base_urls = false` under `[llm]` in `wwyd.toml`). Then a user's own LLM
  address must be a public one, so nobody can make the server call machines on its private
  network. The instance's default LLM backend is not affected.
- **Public instance?** Set `WWYD_AUTH_ALLOW_INSECURE_REDIRECTS=false` (or
  `allow_insecure_redirects = false` under `[auth]`). Apps can then only register https
  redirect addresses (or loopback ones on the user's own machine), so an authorization
  never travels over plain http. The default allows http for LAN setups.
- **Behind your own reverse proxy** (Caddy or nginx in front of `uvx wwyd`, without the
  compose file): set `WWYD_TRUSTED_PROXIES` to the proxy's address (e.g. `127.0.0.1`).
  Otherwise every client appears to come from the proxy's IP, and the per-IP sign-in
  throttles act for everyone at once (wwyd logs a one-time warning). The compose file
  already sets this for its own network.

## D. Connecting your AI apps

The server address for apps is your wwyd address plus `/mcp`:
`http://127.0.0.1:8740/mcp` (A, B) or `https://wwyd.example.com/mcp` (C). Your account
page shows the exact snippets. When an app connects, wwyd asks you to sign in and to choose
what the app may do (`owner`, `read`, or `respond` for chosen audiences).

Checked against the vendors' documentation on 2026-10-18. These screens change often; if
a menu name differs, follow the linked page.

### Claude (claude.ai and Claude Desktop): custom connector

Works only with set-up **C**, because Anthropic's servers make the connection, not your
computer.

1. Pro/Max: **Customize → Connectors → + Add → Add custom connector**. Team/Enterprise: an
   owner adds it under **Organization settings → Connectors → Add → Custom → Web**, then
   members click **Connect**.
2. Enter a name (`wwyd`) and the URL `https://wwyd.example.com/mcp`. Leave the OAuth
   client setting on its default; wwyd supports automatic registration.
3. Click **Connect**, sign in to wwyd and pick a scope.

Source: https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp
(free plans are limited to one custom connector).

### Claude Desktop with a local wwyd (stdio)

For set-up A without a domain. Edit `claude_desktop_config.json` (open it from Claude
Desktop's Settings; macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`, Windows:
`%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "wwyd": {
      "command": "/full/path/to/uvx",
      "args": ["wwyd", "stdio", "--user", "you@example.com"]
    }
  }
}
```

Find the full path with `which uvx` (macOS/Linux) or `where uvx` (Windows). Quit Claude
Desktop completely and reopen it. Over stdio, wwyd uses the data folder directly and needs
no sign-in; if the login does not exist yet it is created.
Source: https://modelcontextprotocol.io/docs/develop/connect-local-servers

### Claude Code

Over HTTP (A, B or C):

```sh
claude mcp add --transport http --scope user wwyd http://127.0.0.1:8740/mcp
```

Then type `/mcp` inside Claude Code, choose `wwyd` and sign in in the browser.

Over stdio (local, no server running):

```sh
claude mcp add --scope user wwyd -- uvx wwyd stdio --user you@example.com
```

For scripts, an API key from your account page works too:
`--header "Authorization: Bearer wwyd_…"`.
Source: https://code.claude.com/docs/en/mcp

### ChatGPT

Works only with set-up **C**. Custom MCP connectors are available on some ChatGPT plans
(Business/Enterprise/Edu with full support; Pro with restrictions) and may need
**developer mode**; a workspace admin may have to allow them. Add a custom app/connector,
enter `https://wwyd.example.com/mcp`, choose OAuth, and sign in to wwyd.

Source: https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt
— **not fully verified on 2026-10-18** (the page could not be loaded; the exact menu path
and plan list must be rechecked before relying on this section).

## E. Updating and backing up

**Back up** the data folder. It contains `wwyd.db` (everything), `secret.key` (needed to
read stored LLM API keys) and optional `wwyd.toml`. Stop wwyd first, or copy all
`wwyd.db*` files together.

`secret.key` is in the backup unless you supply the key yourself with `WWYD_SECRET_KEY`
(the key itself) or `WWYD_SECRET_KEY_FILE` (a path to it). In that case back up the key
separately: without it, stored LLM API keys cannot be read.

- A: copy the folder `~/.wwyd` somewhere safe.
- B: `docker run --rm -v wwyd-data:/data -v "$PWD":/backup alpine tar czf /backup/wwyd-backup.tgz -C /data .`
- C: on the VM, `cd /opt/wwyd && sudo docker compose stop wwyd`, then
  `sudo docker run --rm -v wwyd_wwyd-data:/data -v "$PWD":/backup alpine tar czf /backup/wwyd-backup.tgz -C /data .`
  and `sudo docker compose start wwyd`. Download the file with `scp`.

You can also export your persona at any time with the `export_persona` tool.

**Update:**

- A: `uvx wwyd@latest` (uvx otherwise keeps a cached version).
- B: `docker pull ghcr.io/melnikaite/wwyd:<new version>`, then `docker rm -f wwyd` and
  repeat B.2 with the new version.
  Your data stays in the volume.
- C: set the new version in `WWYD_IMAGE` in `/opt/wwyd/.env`, then
  `cd /opt/wwyd && sudo docker compose pull && sudo docker compose up -d`. (Built from
  source with `WWYD_IMAGE=wwyd:local`: update `WWYD_SOURCE`, run
  `sudo sh -c 'set -a; . ./.env; docker build -t wwyd:local "$WWYD_SOURCE"'`, then
  `sudo docker compose up -d`.)

Database changes are applied automatically when wwyd starts. Back up before updating.

## F. Troubleshooting

| Problem | What to do |
|---|---|
| `command not found: uvx` | Open a new terminal after installing uv. |
| `address already in use` | Something else uses port 8740. Run `uvx wwyd serve --port 8741` and use 8741 everywhere. |
| No setup link printed | An account already exists. Sign in at `/login`. Forgot the password? On the machine running wwyd: `uvx wwyd reset-password you@example.com` (Docker: `docker exec wwyd wwyd reset-password you@example.com`; VM: `cd /opt/wwyd && sudo docker compose exec wwyd wwyd reset-password you@example.com`). |
| Setup link expired | It is valid 24 hours. Restart wwyd to get a new one (only while no account exists). |
| `public_url must use https…` | wwyd is told to use a non-local address without HTTPS. Use set-up C, or remove `public_url` from `wwyd.toml` / `WWYD_PUBLIC_URL`. |
| VM: browser shows a certificate error or nothing | The DNS record is missing or not yet visible, or ports 80/443 are closed. Check with `ping`, then `cd /opt/wwyd && sudo docker compose logs caddy`. Caddy retries by itself. |
| VM: `wwyd-setup-link` prints nothing | Setup is still running: `sudo cloud-init status --wait`, then `sudo docker compose -f /opt/wwyd/docker-compose.yml logs wwyd`. |
| Claude/ChatGPT cannot connect to `127.0.0.1` | Expected: they connect from the internet. Use set-up C, or Claude Desktop/Code with stdio. |
| An app stopped working | Check **Connected apps** on your account page; it may have been revoked. Reconnect it. |
