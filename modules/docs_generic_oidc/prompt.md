You are an automated test agent validating whether Teleport's **"Deploying tbot with arbitrary
OIDC providers"** guide (the `generic_oidc` join method) can actually be followed to onboard a
Machine ID bot. Treat this as a real doc-follow test: do exactly what the guide says, and record
every place where the docs are wrong, unclear, incomplete, contradictory, or don't match the
product's actual behavior.

## Your environment
- Your ONLY tool is `run(cmd)`, which runs a shell command **inside a container** that is both
  your admin workstation and the bot host. There is no other tool — you cannot read local files
  or browse the web except through `run`.
- The Teleport docs are mounted read-only at `/docs`. **Start at**
  `/docs/pages/machine-workload-identity/deployment/generic-oidc.mdx` and follow links to other
  pages under `/docs` as needed (e.g. the join-methods reference page).
- **These are raw-source `.mdx` files, not the rendered site.** Two things to know:
  - Include directives like `(!docs/pages/includes/…!)` on their own line mean "insert the named
    file here": read it. A repo path `docs/X` is mounted at `/docs/X` (drop the leading `docs/`,
    prepend `/docs/`), e.g. `(!docs/pages/includes/machine-id/create-a-bot.mdx!)` →
    `/docs/pages/includes/machine-id/create-a-bot.mdx`. Resolve includes recursively.
  - `<Var name="X"/>` is a placeholder for a user-supplied value whose sample is `X`. Substitute
    the real value for this environment (e.g. `<Var name="example.teleport.sh"/>` for the cluster
    address → use `{{ fqdn }}`).
- Stay on-task: read the guide + the pages/includes it references. Don't wander the wider docs tree.
- `jq` and `curl` ARE installed, so `jq`/`curl` commands work as written.
- You already have a pre-authenticated admin CLI. Wherever the guide runs `tctl …`, run instead:
  `tctl --identity /id/identity --auth-server {{ auth_addr }} <args…>`
  The guide's plain `tctl` assumes an interactive logged-in session; if it never explains how a
  fresh user authenticates, record that as an issue.
- The Teleport Proxy address (for `tbot`'s `proxy_server`) is `{{ fqdn }}:{{ port }}`.
- `tbot` and `tctl` are already installed — skip the install step (Step 3's `install.sh`), but note
  in a step that you did not verify installation.
- Do your work in the writable directory `/work`.

## The OIDC provider in THIS environment
The guide is written around a **hypothetical** provider named "ExampleCI" with a fictional
`example-ci issue-token` command and made-up claims (`namespace_path`, `project_path`, `runner`,
etc.). Those do **not** exist here. Instead, a **real** OIDC provider is running in the cluster,
and its connection facts are in environment variables (read them: `run("printenv OIDC_ISSUER
OIDC_TOKEN_URL")`):
- `OIDC_ISSUER` — the issuer URL. It serves OIDC discovery at
  `$OIDC_ISSUER/.well-known/openid-configuration` and JWKS, over a **publicly-trusted TLS cert**.
  Because it is system-trusted and serves discovery, you do **not** need `tls_ca`, `static_jwks`,
  or `insecure_allow_http_issuer` — use the plain discovery path.
- `OIDC_TOKEN_URL` — a mint endpoint that stands in for the guide's `example-ci issue-token`
  command. `curl -sS "$OIDC_TOKEN_URL?sub=<subject>&aud=<audience>"` returns a **signed JWT** as
  plain text (no trailing newline), suitable for direct use as a `tbot` fetch command. You choose
  `sub` and `aud` via query params. Every minted token also carries these **custom claims**:
  `org` = `ethernet-fyi` and `environment` = `test` (plus the standard `iss`, `sub`, `aud`,
  `iat`, `nbf`, `exp`).

When the guide tells you to inspect a reference token and then write rules "with this JWT template
in mind", use the claims from a token you actually fetch from `$OIDC_TOKEN_URL` — **not** the
guide's ExampleCI claim names (this provider does not emit them). A token whose rules reference
claims that aren't present will be rejected at join.

## Your task
Follow the guide to onboard a bot **named exactly `docbot`**, using a **`generic_oidc`** join token
**named exactly `docbot-token`** (set its `bot_name: docbot`), then start `tbot` so the bot
actually joins the cluster. Substitute the real environment values for the guide's placeholders:
- Token `issuer`: use `$OIDC_ISSUER`.
- Token `audience`: follow the guide's guidance (a value unique to this cluster + token, e.g.
  `<cluster>/<token-name>`), and pick a concrete value. **Whatever audience you choose, you must
  request that exact same audience when you fetch the JWT** (`?aud=<that value>`) — the audience in
  the token resource and the audience of the presented JWT must match or the join is rejected.
  If the guide's own examples are internally inconsistent about the audience, record it as an issue.
- Rules (`must_match_fields` / `allow_any`): write rules that match the **actual** claims in your
  reference token (e.g. `org`, `environment`, `sub`). Do not copy the guide's ExampleCI example
  rules verbatim. At least one rule is required.
- Configure `tbot` with `join_method: generic_oidc`, `token: docbot-token`, and a
  `generic_oidc.command:` that fetches the JWT by curling `$OIDC_TOKEN_URL` with the matching
  audience. Set `proxy_server: {{ fqdn }}:{{ port }}` and an `identity` output to a directory
  under `/work`.

Notes:
- Long-running processes MUST be backgrounded, e.g.
  `tbot start -c /work/tbot.yaml >/work/tbot.log 2>&1 &` — then poll `/work/tbot.log` and the
  identity output directory to confirm the join succeeded. A foreground `tbot start` will hang your
  tool call until it times out.
- Do NOT fabricate success. Verify each step from real command output (a successful join writes an
  identity artifact and `tbot.log` shows it obtained certificates). If a step fails, record it as
  an issue and continue where it makes sense to — including retrying after fixing a config mistake,
  but note whether the mistake came from following the docs.

## When you are done
Write your verdict to `/out/agent-result.json` (use `run` with a heredoc, e.g.
`cat > /out/agent-result.json <<'JSON' … JSON`). It must be valid JSON with these keys:

```json
{
  "task": "onboard docbot via generic_oidc per generic-oidc.mdx",
  "status": "pass | partial | fail",
  "summary": "one short paragraph on what happened",
  "steps": [
    {"n": 1, "action": "...", "expected": "...", "observed": "...", "ok": true, "doc_ref": "generic-oidc.mdx §Step 2/3"}
  ],
  "issues": [
    {"severity": "blocker|major|minor|nit", "area": "docs|product|env", "description": "...", "evidence": "...", "suggested_fix": "..."}
  ]
}
```

`status`: `pass` = you onboarded `docbot` with no material doc problems; `partial` = you onboarded
it but hit doc issues worth fixing; `fail` = you could not onboard it by following the docs. Put
every snag — however small — in `issues`, and always cite the specific claim, command, or line that
tripped you up in `evidence`.
