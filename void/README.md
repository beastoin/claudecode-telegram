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

## CLI Reference

Copy/paste-ready commands for every allowlisted action.

### gh CLI Reference

#### `gh pr`

```bash
void gh pr list --repo owner/repo --state open
void gh pr view 123 --repo owner/repo
void gh pr create --repo owner/repo --title "Add health check" --body "Adds /health endpoint" --base main --head feature/health-check
void gh pr merge 123 --repo owner/repo --squash --delete-branch
void gh pr close 123 --repo owner/repo --comment "Closing in favor of #456"
void gh pr comment 123 --repo owner/repo --body "Smoke test passed."
void gh pr review 123 --repo owner/repo --approve --body "LGTM"
void gh pr diff 123 --repo owner/repo
void gh pr checks 123 --repo owner/repo
void gh pr ready 123 --repo owner/repo
void gh pr edit 123 --repo owner/repo --title "Add readiness probe"
```

#### `gh issue`

```bash
void gh issue list --repo owner/repo --state open
void gh issue view 456 --repo owner/repo
void gh issue create --repo owner/repo --title "Bug: webhook retries forever" --body "Steps to reproduce..." --label bug
void gh issue close 456 --repo owner/repo --comment "Fixed by #789"
void gh issue comment 456 --repo owner/repo --body "Can reproduce on v1.2.3"
void gh issue edit 456 --repo owner/repo --title "Bug: webhook retry loop"
void gh issue reopen 456 --repo owner/repo
```

#### `gh repo`

```bash
void gh repo view owner/repo
void gh repo list owner --limit 20
void gh repo clone owner/repo
```

#### `gh run`

```bash
void gh run list --repo owner/repo --limit 10
void gh run view 123456789 --repo owner/repo
void gh run watch 123456789 --repo owner/repo
```

#### `gh release`

```bash
void gh release list --repo owner/repo --limit 10
void gh release view v1.2.3 --repo owner/repo
```

#### `gh search`

```bash
void gh search repos "terraform aws modules" --limit 10
void gh search issues "repo:owner/repo label:bug timeout" --limit 20
void gh search prs "repo:owner/repo is:open author:octocat" --limit 20
void gh search commits "repo:owner/repo fix race condition" --limit 20
void gh search code "TODO" --repo owner/repo --limit 20
```

#### `gh status`

```bash
void gh status
```

#### `gh label`

```bash
void gh label list --repo owner/repo
void gh label create "needs-triage" --repo owner/repo --color FFAA00 --description "Needs initial triage"
```

### gcloud CLI Reference

```bash
void gcloud projects list

void gcloud compute instances list --project my-project --zones us-central1-a
void gcloud compute instances describe web-1 --project my-project --zone us-central1-a

void gcloud container clusters list --project my-project --location us-central1
void gcloud container clusters get-credentials prod-cluster --project my-project --location us-central1

void gcloud run services list --project my-project --region us-central1
void gcloud run services describe api --project my-project --region us-central1
void gcloud run jobs list --project my-project --region us-central1

void gcloud logging read 'resource.type="cloud_run_revision" AND severity>=ERROR' --project my-project --limit 50

void gcloud storage ls gs://my-bucket/path/
void gcloud storage cp gs://my-bucket/reports/daily.csv ./daily.csv

void gcloud auth activate-service-account ci-bot@my-project.iam.gserviceaccount.com --key-file ./sa-key.json
```

`gcloud storage cp` is download-only by policy: source must be `gs://...`, destination must be local.

### browse CLI Reference

#### Navigation

```bash
void browse open https://example.com
void browse back
void browse forward
void browse reload
```

#### Snapshot / Screenshot

```bash
void browse snapshot
void browse screenshot
```

#### Interaction

```bash
void browse click @e2
void browse dblclick @e3
void browse fill @e4 "search query"
void browse type @e4 "additional text"
void browse press Enter
void browse hover @e5
void browse scroll down 1200
void browse select @e6 "United States"
void browse check @e7
void browse uncheck @e7
void browse upload @e8 ./evidence.png
void browse drag @e9 @e10
```

#### Reading

```bash
void browse get text @e1
void browse get title
void browse get url
void browse get value @e4
void browse get attr @e1 href
void browse get count "button"
void browse get box @e1
```

#### State Checks

```bash
void browse is visible @e1
void browse is enabled @e4
void browse is checked @e7
```

#### Waiting

```bash
void browse wait 2000
```

#### Semantic Locators

```bash
void browse find role button "Sign in"
void browse find label "Email"
void browse find text "Welcome back"
void browse find placeholder "Search docs"
```

#### Tab Management

```bash
void browse tab list
void browse tab new https://docs.github.com
void browse tab close 2
void browse tab switch 1
```

#### Session Management

```bash
void browse session list
void browse session close qa
void browse --session qa open https://cloud.google.com/run
void browse --session qa snapshot
```

#### Display

```bash
void browse set viewport 1366 768
void browse set device "iPhone 14"
void browse set media dark
```

### Denied Commands

Allowlist enforcement blocks anything outside approved subcommands. Deny patterns and global deny regex then block sensitive variants inside approved tools.

```bash
void gh api /user                                      # DENIED - gh api not in allowlist
void gh extension install owner/tool                  # DENIED - gh extension not in allowlist
void gcloud secrets versions access latest --secret x # DENIED - gcloud secrets not in allowlist

void gh auth token                                    # DENIED - explicit command_deny_patterns match
void gh auth login                                    # DENIED - explicit command_deny_patterns match
void gcloud auth print-access-token                   # DENIED - explicit command_deny_patterns match
void gcloud auth print-identity-token                 # DENIED - explicit command_deny_patterns match
void gcloud config set project my-project             # DENIED - explicit command_deny_patterns match

void gh pr list --repo owner/repo --verbosity=debug   # DENIED - global_deny_regex blocks debug verbosity
void gcloud logging read 'severity>=ERROR' --log-http # DENIED - global_deny_regex blocks --log-http
```

## Guidelines

### For AI Agents

- Use `void` as a strict broker: only run `void gh ...`, `void gcloud ...`, and `void browse ...`.
- Always check exit codes. Non-zero means denied by policy or runtime failure.
- Parse structured output where possible (`gh ... --json ...`) instead of scraping text.
- Use the browser loop: `open -> snapshot -> interact -> snapshot` until task completion.
- Use named browser sessions (`--session <name>`) for parallel tasks and close them when done.
- Respect rate limits and timeouts; avoid flooding API/browser commands in tight loops.
- Handle errors explicitly: denied/failed commands return `exit_code=1` and details in `stderr`.

### For Operators

- To allow new commands, update policy in guest config (`/etc/credential-proxy/config.yaml`) and source templates in `void/setup-secure-vm.sh` (`ALLOWED_GH_SUBCOMMANDS`, `ALLOWED_GCLOUD_SUBCOMMANDS`, `ALLOWED_BROWSER_COMMANDS`, `write_proxy_config()`), then re-run `./setup-secure-vm.sh --yes configure` and restart.
- Rotate credentials by updating Bitwarden Secrets Manager values and restarting VM (`./setup-secure-vm.sh --yes restart`) so secrets are re-pulled at boot.
- Monitor command activity and denials in `/var/log/credential-proxy.log` inside the VM.
- Add users with `./setup-secure-vm.sh grant <username>`.
- Back up and recover VM secrets state from:
  - LUKS image: `/var/lib/setup-secure-vm/vms/<vm-name>/secrets.img`
  - LUKS key: `/var/lib/setup-secure-vm/vms/<vm-name>/luks.key` (default vm-name is `secrets-vm`)
- Update Chromium/browser tooling by re-running `./setup-secure-vm.sh --yes configure` (or `fresh` for full rebuild).

### Security Guidelines

- Never share the IPC directory with untrusted host processes.
- Keep the LUKS key file permissions at `0400`.
- Monitor `/var/log/credential-proxy.log` for `DENIED` entries.
- Rotate Bitwarden machine account tokens periodically.
- Keep libkrun and Chromium up to date.
- Review the allowed subcommand/action lists periodically as new `gh`/`gcloud` features ship.

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

## Threat Model

- `void` protects against credential leakage from AI agent workloads at the guest-to-host `stdout`/`stderr` boundary.
- `void` does **not** protect against a compromised host root user. Host root can read the LUKS key, virtiofs/shared mounts, and process memory.
- For hardware-backed protection, seal the LUKS key with TPM.
- The shared IPC directory is the trust boundary for request/response transport; keep directory/file permissions strict and avoid broad host access.

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

54 embedded security tests run without a VM:

```bash
./setup-secure-vm.sh --yes test
```

Test categories:
- **Browser whitelist** (13): ACL enforcement for allowed/denied commands
- **Browser security** (7): env isolation, firewall rules, output scrubbing, session limits, timeouts
- **gh/gcloud ACL** (17): allowed tools, denied tools, deny patterns, subcommand allowlist enforcement
- **Output scrubbing** (9): JWT, GitHub PAT/OAuth, fine-grained PAT, Google tokens, Bearer, AWS, PEM, OpenAI key format
- **Environment isolation** (3): credential passthrough, random var exclusion
- **IPC authentication** (1): unsigned request rejection
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
setup-secure-vm.sh (3385 lines, single file)
  |
  |-- cmd_deps()        Install Rust, Python, gcloud, gh, bws CLI
  |-- cmd_build()       Build libkrunfw + libkrun + krunvm from source
  |-- cmd_create()      Create buildah container + LUKS image + krunvm VM
  |-- cmd_configure()   Write provisioning script, run inside container
  |-- cmd_start()       Unlock LUKS, fetch Bitwarden secrets, boot VM
  |-- cmd_stop()        Stop VM, cleanup mounts
  |-- cmd_clean()       Nuclear 8-step teardown
  |-- cmd_test()        54 embedded Python security tests
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
