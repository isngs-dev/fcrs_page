# First Class Roofing & Solar — Landing Page

A single-page lead-capture landing page for First Class Roofing & Solar
(fcrsga.com), a residential and commercial roofing/solar contractor serving
Alabama and Georgia. Visitors convert into inspection leads through two
paths: a native `tel:` call button and one estimate-request form.

- **`frontend/`** — Astro static site, hand-written CSS, pixel-faithful port
  of the design prototype in `kb/`.
- **`backend/`** — Node/Express API (`POST /api/estimate-request`) that
  validates a submission with zod, appends it as a row to a Google Sheet
  (the only data store), and sends two Gmail-API emails. Deployable both as
  a local long-running server and as a single serverless function.

See [`CLAUDE.md`](CLAUDE.md) for the full project contract — design tokens,
API contract, environment variables, duplicate-submission policy, and
guardrails. That file is the source of truth; this README is just the
quickstart.

## Prerequisites

- Node.js 20.6+ (uses `--env-file`; developed against Node 24)
- A Google Cloud project with the **Gmail API** and **Google Sheets API**
  enabled, and one shared OAuth2 credential authorized for both the
  `gmail.send` and `spreadsheets` scopes (see
  `backend/scripts/get-refresh-token.js` to mint that refresh token)

## Frontend (`frontend/`)

Astro static site.

```bash
cd frontend
npm install
npm run dev      # http://localhost:4321
npm run build    # outputs frontend/dist/
npm run preview  # serve the production build locally
npm test         # Vitest
```

Environment: copy `frontend/.env.example` to `frontend/.env`. The only
variable is `PUBLIC_API_URL` — the absolute origin of the deployed backend
(e.g. `https://api.fcrsga.com`). Leave it unset for local dev; the Vite dev
proxy in `astro.config.mjs` forwards relative `/api/*` calls to
`http://localhost:3000`. In production, leave it unset only if frontend and
backend share one domain with an `/api/*` rewrite rule — otherwise it must
be set or the estimate form cannot reach the backend.

## Backend (`backend/`)

Node/Express API.

```bash
cd backend
npm install
cp .env.example .env   # then fill in real values, see below
npm run dev             # http://localhost:3000, loads .env via --env-file
npm test                # Vitest
```

`npm start` runs the same server without `--env-file` — use this in any
environment (e.g. a serverless host, a container) that injects environment
variables directly rather than mounting a `.env` file.

### Environment variables (`backend/.env`)

Copy `backend/.env.example` and fill in real values. Never commit `.env`.

| Name | Purpose | Default |
| --- | --- | --- |
| `GOOGLE_OAUTH_CLIENT_ID` | OAuth2 client ID of the single shared Google credential (Gmail send + Sheets append). | required |
| `GOOGLE_OAUTH_CLIENT_SECRET` | OAuth2 client secret for that same credential. | required |
| `GOOGLE_OAUTH_REFRESH_TOKEN` | Refresh token authorizing both `gmail.send` and `spreadsheets` scopes. | required |
| `GOOGLE_SHEET_ID` | Spreadsheet ID that receives one appended row per submission. | required |
| `GOOGLE_SHEET_RANGE` | Sheet/tab and range the append targets. | `Leads!A:I` |
| `GMAIL_SENDER_ADDRESS` | Gmail address both emails are sent from (must belong to the OAuth credential). | required |
| `INTERNAL_NOTIFICATION_EMAIL` | FCRS team inbox that receives the lead-detail notification. | required |
| `DUPLICATE_WINDOW_MINUTES` | Time window for the duplicate-submission identity check. | `30` |
| `PORT` | Local dev server port. Ignored under a serverless adapter. | `3000` |
| `ALLOWED_ORIGIN` | Origin allowed by CORS for the browser POST (the deployed frontend URL, or the local dev origin). | required in production |

### Generating the OAuth refresh token

```bash
cd backend
node scripts/get-refresh-token.js <client-id> <client-secret>
```

Follow the printed URL, sign in as the Gmail account that will send mail,
approve **both** permissions on the one consent screen, then paste back the
`code` from the redirected URL. The script prints a single refresh token
covering both scopes — put it in `GOOGLE_OAUTH_REFRESH_TOKEN`.

### Deploying to Render

The repository is configured for [Render](https://render.com) via
[`render.yaml`](render.yaml), which defines two services:

- **`fcrs-api`** — a Node Web Service rooted at `backend/`. It runs as a real
  long-running process (`npm start` → `node src/server.js`); Render injects
  `PORT`, which `server.js` already reads. This is not a serverless deploy.
- **`fcrs-landing`** — a Static Site rooted at `frontend/`, built with
  `npm run build` and served from `frontend/dist/`.

The static site's `/api/*` rewrite rule proxies requests to the `fcrs-api`
service, so the browser stays same-origin and `PUBLIC_API_URL` can remain
unset in production, same as local dev. `render.yaml` is the source of truth
for the deploy config — check it for the exact build/start commands and
rewrite destination.

Backend environment variables are set in the Render dashboard for the
`fcrs-api` service, not in a committed file. `backend/.env.example` is the
authoritative list of variable names to configure.

## Definition of done

`npm run build` and `npm test` green in `frontend/`, and `npm test` green in
`backend/`.

## API surface

`POST /api/estimate-request` is the only endpoint, called by the one hero
estimate-request form. Full field list, validation rules, response shapes,
and the duplicate-submission policy are documented in `CLAUDE.md`.

## Repository layout

```
CLAUDE.md              project contract — design tokens, API contract, guardrails
render.yaml             Render deploy config — fcrs-api + fcrs-landing services
frontend/              Astro site
  .env.example          PUBLIC_API_URL
backend/                Express API + Google Sheets + Gmail
  .env.example           OAuth/Sheets/Gmail config
  scripts/get-refresh-token.js   one-off OAuth consent helper
  src/server.js          local/long-running entry point
kb/                     read-only design handoff — never edit
```
