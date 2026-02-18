# void

Credential-isolated microVM for running `gh`, `gcloud`, and headless Chromium. Secrets stay inside a LUKS-encrypted libkrun VM — only stdout/stderr cross the boundary.

```
void gh pr list                              # GitHub CLI
void gcloud projects list                    # Google Cloud CLI
void browse open https://news.ycombinator.com  # headless browser
void browse snapshot                         # AI-readable page snapshot
```

## How It Works

```
Host                         Shared Volume (virtiofs)        Guest VM (libkrun)

void gh pr list  ──────>  /ipc/requests/req-001.json  ──>  command-proxy-daemon.py
                          /ipc/responses/req-001.json  <──   validates ACL
stdout/stderr  <──────                                       executes command
                                                             scrubs output
```

- **Transport**: JSON files on a virtiofs-mounted shared directory
- **Guest daemon**: Python polling daemon (PID 1), validates whitelist ACL, executes commands, scrubs secrets from output
- **Host client**: `void` bash script installed to `/usr/local/bin/void`
- **Credentials**: Pulled from Bitwarden Secrets Manager at boot, stored in LUKS-encrypted tmpfs inside VM

## Requirements

- Linux with KVM (`/dev/kvm` access)
- Root access (builds libkrun from source, manages LUKS volumes)
- ~5 GB disk for build artifacts + VM image
- 4 GB RAM for VM (2 concurrent browser sessions)

## Quick Start

```bash
# One command from scratch (~30 min first time, builds libkrun from source)
./setup-secure-vm.sh --yes --no-secrets fresh

# With Bitwarden secrets (gh token, gcloud SA key, etc.)
BWS_PROJECT_ID=<uuid> BWS_ACCESS_TOKEN=<token> \
  ./setup-secure-vm.sh --yes fresh

# Grant non-root user access
./setup-secure-vm.sh grant claude
```

## Usage

### gh / gcloud

```bash
void gh pr list
void gh issue view 123
void gcloud projects list
void gcloud compute instances list
```

Credentials are injected at boot from Bitwarden Secrets Manager. The daemon blocks sensitive commands:

| Blocked Command | Reason |
|----------------|--------|
| `gh auth token` | Would leak PAT |
| `gh auth login` | Would overwrite creds |
| `gcloud auth print-access-token` | Would leak token |
| `gcloud auth print-identity-token` | Would leak token |
| `gcloud config set` | Could redirect auth |
| `gcloud auth login` | Would overwrite creds |

### Browser

```bash
void browse open https://example.com          # navigate
void browse snapshot                          # AI-readable accessibility tree
void browse screenshot                        # PNG screenshot
void browse click @e2                         # click element ref from snapshot
void browse fill @e3 "search query"           # type into input
void browse get text @e1                      # extract text
void browse get title                         # page title
void browse tab list                          # list open tabs
void browse --session qa open https://docs.github.com  # named session
```

Uses [Vercel agent-browser](https://github.com/vercel-labs/agent-browser) (headless Chromium with snapshot-based AI workflow).

## Security Model

Three independent defense layers:

### 1. Whitelist-Only ACL (38 allowed browser commands)

Only explicitly whitelisted commands execute. Everything else is denied — no blacklist.

**Allowed categories**: navigation, snapshot, screenshot, interaction (click/fill/type/press/hover/scroll), reading (get text/title/url/value), state checks (is visible/enabled/checked), waiting, semantic locators, tab management, session management, display settings.

**Explicitly denied**: `evaluate`, `cookies`, `localStorage`, `sessionStorage`, `state`, `console`, `errors`, `route`, `requests`, `set credentials`, `set headers`, `get html`, `trace`, `highlight`.

### 2. FORBIDDEN_TOKENS Defense-in-Depth

Even if a command matches the whitelist, reject if any argument contains: `cookie`, `storage`, `eval`, `evaluate`, `exec`, `script`, `credential`, `header`, `route`, `intercept`, `proxy`, `console`, `errors`, `trace`, `state`, `html`.

### 3. URL Validation + Network Isolation

- **HTTPS only** — `http://`, `file://`, `data://` all denied
- **Private IP blocking** — 10.x, 172.16-31.x, 192.168.x, 127.x, 169.254.x, `::1`
- **Metadata endpoint blocking** — `169.254.169.254`, `metadata.google.internal`
- **UID-scoped iptables** — `VOID_BROWSER_EGRESS` chain allows only port 443 outbound for the `void` OS user

### Environment Isolation

- Browser runs as dedicated `void` OS user (no login shell)
- Sanitized env: `PATH`, `HOME`, `LANG` only — no `GH_TOKEN`, no `GOOGLE_APPLICATION_CREDENTIALS`
- Cgroup v2 limits: 1.5 GB memory, 2 CPU, 192 pids max
- Max 2 concurrent browser sessions

### Output Scrubbing

All output is scrubbed before crossing the VM boundary:
- JWTs (`eyJ...`)
- GitHub tokens (`ghp_`, `gho_`, `ghs_`, `ghu_`, `ghr_`)
- Google OAuth tokens (`ya29.`)
- Bearer tokens
- AWS access keys (`AKIA...`)
- PEM private keys
- URL query parameters with secret names (`?token=`, `?api_key=`, etc.)

## Subcommands

| Command | Description |
|---------|-------------|
| `fresh` | Nuclear clean + full rebuild + start (one command from scratch) |
| `deps` | Install host dependencies (Rust, Python, gcloud, gh, bws) |
| `build` | Build libkrunfw + libkrun + krunvm from source |
| `create` | Create LUKS volume + microVM |
| `configure` | Configure guest proxy daemon, firewall, and CLI tools |
| `start` | Start VM (unlock LUKS, pull Bitwarden secrets, boot daemon) |
| `stop` | Stop VM and cleanup runtime state |
| `restart` | Stop + start (re-pulls secrets) |
| `grant <user>` | Grant non-root user access to `void` CLI |
| `status` | Show VM/runtime status |
| `test` | Run embedded security tests (48 tests) |
| `clean` | Nuclear teardown: unmount LUKS, delete VM, shred secrets |
| `all` | Run deps + build + create + configure |

## Global Flags

```
--dry-run              Print commands without executing
--verbose              Print detailed diagnostics
--yes                  Auto-confirm destructive operations
--no-secrets           Skip Bitwarden secrets injection on start
--keep-build           Skip removing build artifacts during clean/fresh
--vm-name NAME         VM name (default: secrets-vm)
--cpus N               VM CPU count (default: 4)
--mem N                VM memory in MB (default: 4096)
--no-color             Disable ANSI colors
```

## Testing

48 embedded security tests run without a VM:

```bash
./setup-secure-vm.sh --yes test
```

Test categories:
- **Browser whitelist** (13): ACL enforcement for allowed/denied commands
- **Browser security** (7): env isolation, firewall rules, output scrubbing, session limits, timeouts
- **gh/gcloud ACL** (14): allowed tools, denied tools, deny patterns
- **Output scrubbing** (7): JWT, GitHub PAT/OAuth, Google tokens, Bearer, AWS, PEM
- **Environment isolation** (3): credential passthrough, random var exclusion
- **Host proxy** (4): tool allowlist, rejection, argv JSON, browse routing

## Bitwarden Secrets Setup

Only needed if injecting secrets at boot (skip with `--no-secrets`):

1. Go to [vault.bitwarden.com](https://vault.bitwarden.com) > Secrets Manager
2. Create a Project (copy the UUID = `BWS_PROJECT_ID`)
3. Add secrets as key-value pairs (e.g., `GH_TOKEN` = `ghp_xxx`)
4. Create a Machine Account, assign it to the project
5. Generate an Access Token (= `BWS_ACCESS_TOKEN`)

For multi-line secrets (e.g., gcloud SA key JSON), store as separate `.json` secrets — the script handles them automatically.

## Architecture

```
setup-secure-vm.sh (3034 lines, single file)
  |
  |-- cmd_deps()        Install Rust, Python, gcloud, gh, bws CLI
  |-- cmd_build()       Build libkrunfw + libkrun + krunvm from source
  |-- cmd_create()      Create buildah container + LUKS image + krunvm VM
  |-- cmd_configure()   Write provisioning script, run inside container
  |-- cmd_start()       Unlock LUKS, fetch Bitwarden secrets, boot VM
  |-- cmd_stop()        Stop VM, cleanup mounts
  |-- cmd_clean()       Nuclear 8-step teardown
  |-- cmd_test()        48 embedded Python security tests
  |
  |-- write_proxy_daemon()        Guest-side Python daemon (PID 1)
  |-- write_proxy_config()        ACL config (YAML)
  |-- write_firewall_script()     iptables rules (guest)
  |-- write_host_proxy_script()   /usr/local/bin/void (host)
  |-- write_guest_provision_script()  apt install, user setup, Chromium
```

## Pen Test Results

17 attack vectors tested against `void browse`, all blocked:

| Vector | Result |
|--------|--------|
| `evaluate "JSON.stringify(process.env)"` | Denied (unsupported action) |
| `cookies` / `localStorage` / `console` | Denied (unsupported action) |
| `state save` / `get html` / `set credentials` | Denied (unsupported action) |
| `wait --fn` / `wait --download` | Denied by policy |
| `file:///run/secrets/env` | Only https:// allowed |
| `http://example.com` | Only https:// allowed |
| `https://169.254.169.254/meta-data/` | Denied URL address |
| `https://metadata.google.internal/` | Denied URL hostname |
| `https://10.0.0.1/` / `192.168.1.1` / `127.0.0.1` / `localhost` / `[::1]` | All denied |
| `$(cat /run/secrets/env)` in URL | URL-encoded literal, no execution |
| `"; cat /run/secrets/env #` in URL | Literal string, DNS failure |
| `https://httpbin.org/headers` | Forbidden token: "header" |
