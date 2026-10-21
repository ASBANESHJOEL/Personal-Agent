# Personal Agent

Personal Agent is a responsive control room with a fail-closed FastAPI policy boundary. Sample dashboard cards are explicitly demo data and are not live platform activity.

## Project layout

```text
app/                     Next.js app shell
components/              Dashboard UI, authenticated discovery panel, and frontend tests
backend/app/              FastAPI API, public discovery adapters, auth, state, policy gateway
backend/tests/            API and safety tests
supabase/migrations/      Versioned PostgreSQL schema with RLS
```

## Run locally

Requires Node.js 20+, npm, and Python 3.11+.

1. Create a root `.env.local` for Next.js and a root `.env` for the backend using `.env.example` as a placeholder reference. Set the real project URL and publishable key only in `.env.local`; set server verification values only in `.env`.
2. Start the backend:

   ```powershell
   cd backend
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   uvicorn app.main:app --reload --port 8000
   ```

3. In another terminal at the repository root:

   ```powershell
   npm install
   npm run dev
   ```

4. Open http://localhost:3000. API health: http://localhost:8000/health; readiness: http://localhost:8000/ready.

## Configuration and authentication

The browser API client uses `NEXT_PUBLIC_API_BASE_URL` and defaults to `http://localhost:8000`. Requests time out after five seconds. Next.js reads the repository-root `.env.local`; FastAPI reads the repository-root `.env` (also supports server environment variables). This avoids competing env files under `backend/`. Set `CORS_ORIGINS` to the exact comma-separated frontend origins allowed to call the API.

Sign in with an existing Supabase Auth email/password account from the dashboard. In the repository-root `.env.local`, set `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` from the Supabase project's API settings. The publishable key is intended for browser use; never put a service-role key, database credential, or signing secret in a `NEXT_PUBLIC_*` variable. If either public value is missing, the dashboard shows a setup-needed state and protected controls remain unavailable. Restart Next.js after changing `.env.local`.

The backend verifies access tokens using the configured Supabase project's trusted JWKS at `${SUPABASE_URL}/auth/v1/.well-known/jwks.json` by default. The current project uses ECC/P-256 (ES256), so leave `SUPABASE_JWT_SECRET` blank. Set `SUPABASE_JWKS_URL` only for a trusted endpoint override. `SUPABASE_JWT_SECRET` exists only for legacy HS256 projects and stays server-side. The verifier pins the issuer to `SUPABASE_URL` and requires the authenticated audience. `.env.example` contains placeholders only; `.env.local` and `.env` are ignored by Git.

### Persistent state and storage modes

`AGENT_STATE_STORAGE` selects the backend for all application-owned persistent state: Global Pause, action policies, approvals, discovery items, schedule settings and scheduled run history. It defaults to `sqlite` only for local development and tests. Set `AGENT_STATE_STORAGE=supabase` for the API and worker. The API uses the publishable key in the `apikey` header plus the exact verified signed-in user's access token in `Authorization: Bearer ...`; Supabase runs requests as `authenticated` and applies owner-scoped RLS. No service-role key is used by API requests.

When `APP_ENV=production`, startup and readiness fail if Supabase storage is not selected/configured or if `SUPABASE_SERVICE_ROLE_KEY` is present in the API environment. Production never initializes SQLite or silently falls back to it. Local SQLite remains available only when explicitly running development/test mode. `/ready` reports configuration separately from authenticated state verification; a successful signed-in request is the evidence that user-scoped access worked.

The API inserts conservative action-policy defaults using the authenticated user's JWT without overwriting existing choices. Policy authorization reads Global Pause, the owner-scoped policy, and any matching action/approval from Supabase; any missing or uncertain dependency fails closed. Approval rows must reference a pending action owned by the same user. Public posting/comment actions still require explicit approval and no execution path is implemented. Queue creation, execution attempts, and audit writes have no browser-facing operation or authenticated write grants in this milestone.

To preview legacy SQLite records for one user, run from `backend` with the same local `DATABASE_URL` that points at the source file:

```powershell
uv run --python .\.venv\Scripts\python.exe -- python -m app.import_sqlite_state --user-id YOUR_SUPABASE_AUTH_USER_UUID
```

This is a read-only dry run by default. It previews Global Pause, policy and discovery rows. To apply, repeat the command with `--apply`: `uv run --python .\.venv\Scripts\python.exe -- python -m app.import_sqlite_state --user-id YOUR_SUPABASE_AUTH_USER_UUID --apply`. Legacy approvals are skipped because the old local schema has no `action_queue.action_id` relationship; re-approval will require an authenticated approval workflow, which is not implemented yet. The utility prompts for a hidden user access token, verifies it against the configured Supabase issuer/JWKS, requires its subject to match `--user-id`, and imports with that user's JWT. Duplicate policy/discovery rows are ignored. The source database and backups are never modified or removed. Import the Global Pause value before the first authenticated cloud state request; thereafter Supabase is authoritative and runtime requests never read SQLite.

### Public discovery (Milestone 4)

The Discover page calls authenticated FastAPI routes: `POST /api/v1/discovery/run`, `GET /api/v1/discovery`, and `PATCH /api/v1/discovery/{item_id}`. The backend retrieves public GitHub repository search results and Hugging Face model, dataset, and Space metadata; GitHub access is anonymous unless the optional server-only `GITHUB_TOKEN` is set. The token needs no write scopes. Search is bounded to at most 30 results per source per page, source calls have an eight-second default timeout (`DISCOVERY_REQUEST_TIMEOUT_SECONDS`), and per-source rate limits/timeouts are reported independently. Search is read-only; discovery records are only persisted to this application's state store. Ranking is deterministic and based on provider metadata, topic/search matches, and recency; the displayed score is an informational ordering aid, not a validated quality measure.

In SQLite mode discoveries are stored in the explicitly selected local SQLite database. In Supabase mode the backend forwards the verified user's bearer token to PostgREST and uses owner-scoped rows/RLS; cloud errors do not fall back to SQLite. Apply `supabase/migrations/202610200002_discovery_content_items.sql` after the initial and cloud-state schema migrations before using discovery with Supabase. This migration adds nullable discovery metadata columns and owner-scoped authenticated insert/update policies to `content_items`; authenticated updates are limited to the status column and discovery rows. It does not remove existing rows or policies. Check the project's migration history before applying. No migration is applied automatically by the app.

Provider metadata is treated as untrusted display text. Discovery does not create posts/comments, stars, follows, or any other external platform actions.

### Apply the database migration

The forward-only Global Pause migration is `supabase/migrations/202610190002_cloud_agent_state.sql`. It adds the one-time SQLite import marker, grants only `SELECT`, `INSERT`, and `UPDATE` to `authenticated`, and applies owner-scoped RLS policies. Check the linked project's migration history before applying migrations; this README does not assert their current remote application status.

For a new project, apply migrations in timestamp order after checking which are already present: `202610190001_initial_schema.sql`, `202610190002_cloud_agent_state.sql`, `202610200001_restrict_state_initializer.sql`, `202610200002_discovery_content_items.sql`, `202610200003_scheduled_discovery.sql`, and `202610200004_authenticated_agent_state_access.sql`. Migration 004 revokes inherited browser-role table privileges from the action tables and grants only owner-scoped policy-default and approval operations; it does not give browser roles queue, execution-attempt, or audit-log writes. Review the SQL first and use the project's normal migration workflow. No migration is applied automatically by the app. No database password or direct connection string is required by the application runtime; migration tooling may require CLI authentication.

### Environment variables and file locations

| File | Variables |
| --- | --- |
| Repository-root `.env.local` (Next.js, public) | `NEXT_PUBLIC_API_BASE_URL`, `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` |
| Repository-root `.env` (FastAPI) | `AGENT_STATE_STORAGE`, `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_JWKS_URL` (optional), `SUPABASE_JWT_SECRET` (HS256 only), `SUPABASE_REQUEST_TIMEOUT_SECONDS`, `DISCOVERY_REQUEST_TIMEOUT_SECONDS`, optional server-only `GITHUB_TOKEN`, `CORS_ORIGINS`, `APP_ENV` |

The frontend publishable key is public by design. Never put Supabase secret/service-role keys, JWT signing secrets, or database credentials in frontend variables. `.env.example` is only a placeholder template.

## Safety model

- Each authenticated workspace gets a persistent global pause state that defaults to on. It is checked for every policy decision and scoped to the authenticated user.
- Pause state and action policy are scoped to the authenticated user. Missing auth, state, or policy fails closed.
- The policy route only evaluates authorization and returns `executed: false`; no platform executor exists.
- Public comments and posts require explicit approval, even if a policy is misconfigured.
- GitHub starring is explicitly unsupported.
- AI-generated proposals are modeled separately from execution attempts in the schema.

## Tests

```powershell
# Backend (use the existing uv-managed environment)
uv run --python .\backend\.venv\Scripts\python.exe -- python -m pytest -q

# Frontend
npm install
npm test
```

Unit tests use mocked provider and PostgREST responses and do not require cloud credentials. The optional live Supabase integration has not been run. GitHub/Hugging Face discovery has not been exercised against live provider APIs in this project session. No platform write integrations are implemented.

## Scheduled discovery (Milestone 5)

Scheduling is an explicit per-user opt-in stored in `public.discovery_schedules`; a missing row means disabled. The signed-in user can change only their own `enabled` field through `GET` and `POST /api/v1/discovery/schedule`. Recent results and run history are stored in Supabase `agent_runs`. The default run time is daily at **03:00 UTC (08:30 India Standard Time)**; configure `SCHEDULED_DISCOVERY_HOUR_UTC` consistently on the API and Cron services if changing it. `SCHEDULED_DISCOVERY_WORKER_DEPLOYED` is an operator-provided UI indicator, not a live platform check.

The worker is a one-shot process: `python -m backend.app.discovery_worker`. It requires `AGENT_STATE_STORAGE=supabase`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, and `SUPABASE_SERVICE_ROLE_KEY`. Keep the service-role key only in the isolated Render Cron service environment. Never set it on the API service or frontend. The worker checks enabled schedules, verifies Global Pause, writes idempotent discovery rows without resetting saved/dismissed items, and records run status/lease data. Cron schedule configuration is disabled until an operator creates the Render Cron service; user schedules also default to disabled.

## Render preparation

The root Dockerfile starts the API on `0.0.0.0:${PORT:-8000}`. For a Render Web Service, use the repository root as the Docker context and the root Dockerfile; Render supplies `PORT`. Use `/health` as the liveness health-check path. The API requires `APP_ENV=production`, `AGENT_STATE_STORAGE=supabase`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, trusted JWT verification configuration, and exact `CORS_ORIGINS`. The API deliberately fails startup/readiness if SQLite is selected, Supabase public configuration is missing, or `SUPABASE_SERVICE_ROLE_KEY` is accidentally provided to that service.

For an independent Render Cron Job, build from the same root Dockerfile and override its start command to `python -m backend.app.discovery_worker`. Configure the cron schedule only after reviewing cost and run frequency. Put `SUPABASE_SERVICE_ROLE_KEY` only in this Cron service's environment. No Render service has been created or deployed in this task.

**Frontend variables:** `NEXT_PUBLIC_API_BASE_URL`, `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`.

**API Web Service variables:** `APP_ENV=production`, `AGENT_STATE_STORAGE=supabase`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_JWKS_URL` (optional trusted override; normally omit), `SUPABASE_JWT_SECRET` only for legacy HS256 signing (omit for asymmetric ES256), `CORS_ORIGINS`, `SUPABASE_REQUEST_TIMEOUT_SECONDS`, `DISCOVERY_REQUEST_TIMEOUT_SECONDS`, `SCHEDULED_DISCOVERY_HOUR_UTC`, and optional `GITHUB_TOKEN`. Do not configure `SUPABASE_SERVICE_ROLE_KEY` here.

**Cron Job variables:** `APP_ENV=production`, `AGENT_STATE_STORAGE=supabase`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_REQUEST_TIMEOUT_SECONDS`, `DISCOVERY_REQUEST_TIMEOUT_SECONDS`, `SCHEDULED_DISCOVERY_HOUR_UTC`, and optional `GITHUB_TOKEN`.

**Vercel variables:** `NEXT_PUBLIC_API_BASE_URL` set to the Render API URL, `NEXT_PUBLIC_SUPABASE_URL`, and `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`. Never expose the service-role key, database credentials, or JWT signing secret through `NEXT_PUBLIC_*` variables.
