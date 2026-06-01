# camwatch-demo

> **Archived and no longer maintained.** This public demo is retired. The
> camwatch service is now hosted on Cloudflare with login control, so a
> separate no-account demo is no longer needed. The live deployment
> (`camwatch-demo.leidevs.com`) and its Cloudflare Pages project have been
> removed. The notes below are kept for reference only.

Static snapshot of [camwatch](https://github.com/leochen4891/camwatch) for public demo.
Live at **https://camwatch-demo.leidevs.com/**.

The live camwatch service is behind Cloudflare Access; this demo lets anyone
poke at the UI without an account by baking a 3-day window of real captures
into a fully static site on Cloudflare Pages.

## What it ships

- The camwatch index page rendered once, with the most recent three days of
  passes baked in (clips + thumbs + per-pass trajectory JSONL).
- The same per-pass speed chart, grid overlay, video player, heatmap, and
  histogram as the live UI.
- A demo banner across the top; filter changes and write actions (annotate /
  delete / settings) are inert and surface a "read-only demo" toast.
- Live preview pane shows the most recent frame as a still (no MJPEG).

## Build

Requires the [camwatch](https://github.com/leochen4891/camwatch) source
checkout next to this repo so the script can read its DB and recordings:

```sh
# adjacent layout
github/
  camwatch/        # the live service
  camwatch-demo/   # this repo
```

Then:

```sh
cd ../camwatch
uv run --no-project python ../camwatch-demo/scripts/build_demo.py \
    --source . --out ../camwatch-demo/dist
```

Flags:
- `--today YYYY-MM-DD` — date-lock the snapshot (default: today, local).
- `--days N` — how many trailing days to include (default: 3).
- `--end YYYY-MM-DDTHH:MM:SS` — explicit exclusive upper bound. Useful for
  excluding the most recent captures (e.g., a person on the lawn) without
  changing the trailing-day window.

Output goes to `dist/` (gitignored). Inspect locally with:

```sh
cd ../camwatch-demo
npx wrangler@latest pages dev dist
```

## Deploy (Cloudflare Pages)

One-time project setup:

```sh
export CLOUDFLARE_API_TOKEN=...
export CLOUDFLARE_ACCOUNT_ID=...
npx wrangler@latest pages project create camwatch-demo --production-branch main
```

Each deploy:

```sh
npx wrangler@latest pages deploy dist \
    --project-name camwatch-demo --branch main --commit-dirty=true
```

## Custom domain (one-time)

Attach the domain to the Pages project:

```sh
ACCT=$CLOUDFLARE_ACCOUNT_ID
curl -sS -X POST -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
    -H "Content-Type: application/json" \
    --data '{"name":"camwatch-demo.leidevs.com"}' \
    "https://api.cloudflare.com/client/v4/accounts/$ACCT/pages/projects/camwatch-demo/domains"
```

Add the matching CNAME (proxied) in the `leidevs.com` zone:

```sh
ZONE=$(curl -sS -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
    "https://api.cloudflare.com/client/v4/zones?name=leidevs.com" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"][0]["id"])')

curl -sS -X POST -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
    -H "Content-Type: application/json" \
    --data '{"type":"CNAME","name":"camwatch-demo","content":"camwatch-demo.pages.dev","ttl":1,"proxied":true,"comment":"camwatch read-only demo"}' \
    "https://api.cloudflare.com/client/v4/zones/$ZONE/dns_records"
```

The token needs **Account: Pages — Edit** + **Zone: DNS — Edit** on
`leidevs.com`. Cloudflare provisions the cert via HTTP-01 once the CNAME
resolves; usually live within a few minutes.

## Daily refresh

Build + deploy in one shot. On the original author's machine the Cloudflare
credentials live in the macOS Keychain — the global `~/.claude/CLAUDE.md`
documents the keychain entry, so Claude Code can rebuild and redeploy on
request without seeing the token:

```sh
export CLOUDFLARE_API_TOKEN="$(security find-generic-password -s cf-token -a claude -w)"
export CLOUDFLARE_ACCOUNT_ID=b10e21f6d7d6d6344afa5e5f86def18e

cd ../camwatch && uv run --no-project python \
    ../camwatch-demo/scripts/build_demo.py --source . --out ../camwatch-demo/dist
cd ../camwatch-demo && npx wrangler@latest pages deploy dist \
    --project-name camwatch-demo --branch main --commit-dirty=true
```

Add `--end 2026-05-09T14:23:51` (or any cutoff) to the build command if you
need to exclude the most recent captures.

## How it works

- Imports camwatch's render helpers (`render_pass`, `_build_histogram`,
  `_build_heatmap`) and runs them in-process against a frozen `_today_local`
  so the heatmap window matches the data being shipped.
- Renders `index.html` and `_pass_list.html` straight from the upstream Jinja
  templates with `PAGE_SIZE = 10000` so all rows fit on one page (paginator
  hides itself).
- Patches the rendered HTML for the static layout: `?big=1` -> `_big`
  (Cloudflare Pages ignores query strings during file lookup); demo banner;
  static still in the live preview pane; toast shim that intercepts htmx
  POSTs.
- Materializes per-pass binary assets at `/passes/{id}/{clip,thumb,thumb_big,trajectory.jsonl}`
  by copying from the camwatch `recordings/` and `events/` directories.
- Drops singleton API responses at `/api/homography`, `/api/status`,
  `/status-badge`, and `/preview/stream` (the most recent thumbnail served as
  a still JPEG).
- Ships `_headers` so extensionless paths get the right Content-Type at the
  Cloudflare edge.
