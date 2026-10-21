# Changelog

## Creative analysis causes and ad set location search — 2026-09-10

Creative library analysis (required before launch) no longer reports every
failure as "an admin can check the Gemini configuration". Empty, overlong or
out-of-enum model labels are fitted to the metadata schema instead of failing
the creative; safety blocks, rejected media, rate limits, timeouts and media
download problems name their cause; a transient 429/5xx retries once; only key
problems (401/403, invalid key) point at Settings → Integrations (issue #58).

Ad set location search now queries Meta's location catalog
(`/search?type=adgeolocation`). It called the ad account's `/targetingsearch`
edge, which returns interests and behaviors only, so countries, states and
cities could not be added or excluded (issues #8, #27).

Tests: backend 643 passed, 1 xpassed on isolated local PostgreSQL (10 new,
each failing before the fix); no frontend changes. Provider responses are
simulated in tests; live Gemini and Meta behavior is unverified until the media
team retests on the deployed build.

## Instagram page identity default and honest Gemini errors — 2026-09-10

Ads no longer require a linked Instagram account when Instagram placements are on:
the creative step defaults to "Use Facebook Page (default)", the wizard and the
publish preflight accept the page identity, and the creative sets
`use_page_actor_override` so Meta runs Instagram placements under the Page
(issue #19).

Copy generation and Ad Remix Gemini failures now report their real cause instead
of pointing every failure at Settings → Integrations: provider safety blocks and
rejected requests surface specific messages, rate limits retry once and then say
to wait, and only key-level failures (401/403/402) change the stored connection
status. (Creative library analysis uses a separate client; see the entry above.)

[PR #59](https://github.com/jasonakatiff/theleadrouter-ad-studio/pull/59).
Tests: 12 backend unit tests passed (6 new for the provider boundary), 98
frontend unit tests and the frontend build passed locally.

## Creative analytics and public production alignment — 2026-09-08

Bring creative provenance, external uploads, Google/TikTok analytics, metadata patterns, request-volume settings, and posting recovery into the public release. Preserve installer setup, encrypted provider settings, current branding, and the public security fixes. Join both released database histories with an additive merge and retain existing users, credentials, and ads.

Remove deployment-specific URLs and tracked upload artifacts from published source. Add a redacted secret scan and a public-content hygiene gate. The private repository history is excluded.

[Cutover plan and acceptance status](docs/plans/public-production-cutover.md). Local verification: 613 backend tests and both upgrade baselines passed, 98 frontend unit tests and build passed. Secret and content scans passed. Hosted cutover and container CI remain pending until release verification.

## Local v2 setup and Docker Compose — 2026-09-08

Replace the legacy secret-writing wizard with process-environment validation and the shared v2 bootstrap. Repeated setup preserves the owner and saved encrypted provider credentials. Repair fresh Compose startup, add the sync worker and readiness ordering, keep database/media/dependency data in named volumes, and restrict development HTTP ports to loopback. Update the README and add a [local development guide](docs/deployment/local-development.md).

[Plan and test status](docs/plans/local-installers-v2.md): eight standalone setup checks and four isolated PostgreSQL integration tests passed locally, including actual setup reruns and provider decryption. Shell syntax and Compose configuration checks passed. All required PR checks passed, including the actual Compose stack, frontend proxy, worker, and container recreation with disposable storage. CI verified persistent owner/JWT/setup state, encrypted provider decryption, and media; all test containers, volumes, and networks were removed. Frontend checks passed 85 unit and 41 browser tests; five installation/telemetry browser tests used simulated AI. The 27 existing browser tests requiring a separate live app were skipped. No production data or paid provider calls used.

## Public repository routing for agents — 2026-09-08

Add canonical agent guidance for `jasonakatiff/theleadrouter-ad-studio`, require
fetch/push and GitHub visibility checks, and treat the private repository as a
historical reference. Align Claude instructions with the public v2 setup and
isolated test data. Verify instruction links and repository identity; required
GitHub checks apply to the documentation PR.

## Security: critical dependencies and exception privacy — 2026-09-08

- Patch python-jose, Vitest/coverage and the transitive shell-quote dependency.
- Campaign preflight hides unexpected provider exception details while preserving
  application-authored campaign and budget validation guidance.
- Restrict workflow tokens and pin Codecov to a verified upstream commit.
- [Plan and test status](docs/plans/security-dependencies-and-alerts.md): phase 0
  reproduced three failures; 14 focused backend tests and 85 frontend tests with
  coverage passed, production frontend build passed, and npm audit reports zero
  critical entries. GitHub CI passed 512 backend tests with one existing XPASS,
  41 frontend browser tests, five installation/telemetry browser tests, container
  startup/persistence and CodeQL checks. The 27 existing browser tests needing a
  separate live app were skipped.
- [Review of all 21 remaining CodeQL alerts](docs/security/remaining-codeql-review-2026-09-08.md)
  distinguishes repaired issues from false positives supported by source/tests.

## Public installation guidance and worker destination — 2026-09-08

Require an explicit API origin in the downloadable plugin worker so a missing setting cannot send its key to the private installation. Remove private deployment URLs from the API, plugin, and telemetry guides. Replace the obsolete Railway deployment walkthrough with the four-service installer reference, correct README API routes, and use the v2 bootstrap and sync worker in manual setup. Mark the legacy setup wizard and Compose startup as unsupported for fresh v2 installations pending repair.

Verification: three worker configuration regression tests passed after reproducing the missing-origin failure; GitHub Markdown rendering, document links, all seven README API routes, and Railway configuration checks passed. Worker requests were mocked; no production database or provider calls were made.

## Security: media uploads and Graph reads — 2026-09-08

- Facebook uploads reject private network destinations and server file paths,
  reuse DNS-pinned bounded downloads, and clean up temporary files on failure.
- Graph video and delivery reads validate resource paths and reject redirects;
  legacy video requests carry access tokens in headers.
- Telemetry redaction avoids excessive regex backtracking on hostile strings.
- [Plan and test status](docs/plans/security-codeql-findings.md): 139 focused tests
  passed; GitHub CI passed 505 backend tests with one existing XPASS, plus
  frontend, installation, container persistence, and CodeQL checks. Includes evidence-based
  triage of all 17 critical/high CodeQL findings.

## README installation paths — 2026-09-08

Make the Railway one-click preview the Quick Start and Deployment path for **theLeadRouter — Ad Studio**. Label local development separately, add the missing documentation navigation target, and document Render, Northflank, DigitalOcean, and Coolify as future installation options with official sources. Railway remains the only published installer. Verification: GitHub Markdown rendering, README anchors and relative links, and the Railway configuration check passed.

## README and Railway branding — 2026-09-08

Complete **theLeadRouter — Ad Studio** branding in README product links, footer, and setup examples while preserving author credits. Rename the existing Railway project and one-click template, and point the hosted template at public `main`. Fresh cloud verification passed for all four services on `fcf30f1`, owner login/setup, backend readiness, worker heartbeat, and live browser branding. Disposable projects were removed. README rendering and installer configuration checks passed. See the [completed preview release plan](docs/deployment/v2-railway-release.md).

## Ad Studio naming — 2026-09-08

Use **theLeadRouter — Ad Studio** for the public product and `theleadrouter-ad-studio` for its repository. Update the UI, API title, guides, downloads, package metadata, and Railway source references. Existing workspace branding overrides and integration identifiers remain compatible. See the [release and rename plan](docs/deployment/v2-railway-release.md). Verification: 85 frontend tests, production build, scoped lint, installer checks, public login smoke, and desktop/mobile branding passed. Hosted Railway naming is recorded separately from checked-in configuration.

## Railway installer preview — 2026-09-08

An unlisted [Deploy on Railway preview](https://railway.com/deploy/rNhJ3h) creates the four connected services and persistent storage from two owner fields. Sign in to configure AI keys in the first-run wizard or Settings → Integrations.

Cloud checks passed for fresh owner setup, independent credentials, worker heartbeat, encrypted-key decryption, and record/media persistence after restart and update. Fixed nginx startup under Railway command overrides and clear Gemini invalid-key feedback. Paid generation and nontechnical pilot acceptance remain open.

## 2.0.0-rc.1 — theLeadRouter Ad Builder & Manager (unreleased)

Rename the public product from BreadWinner / Facebook Ad Builder to **theLeadRouter — Ad Builder & Manager**. Update the app wordmark, tab title, favicon, API identity, help text, downloadable guides, installer display text, and public README. Keep configured workspace branding and existing integration identifiers compatible. See [product naming](docs/brand-guidelines.md). Branding verification: 85 frontend tests, 12 backend API-key/documentation tests, production frontend build, scoped ESLint, and desktop/mobile browser checks passed. Browser account and help data were simulated; the API docs bundle test used isolated PostgreSQL. This entry does not mark the v2 release published.

Add a resumable setup wizard, encrypted AI credentials in Settings, one-time owner bootstrap, persistent media and a four-service Railway scaffold. Preserve the current workspace, diagnostics and plugin features. Local validation passed 410 backend tests plus one existing XPASS, 78 frontend unit tests and a production build. The five installer/telemetry browser checks passed in CI with simulated AI. The unlisted Railway template has passed automated cloud installation checks. Funded live generation and marketplace publication remain pending. See the [installer release procedure](docs/deployment/railway-template-maintainer.md).

## [2026-09-05] Consolidate remaining local (breadWinner) assets

### Added
- Playwright end-to-end suite ported from the private repo: 10 spec files, 42 tests (auth, session refresh, brands CRUD, Facebook campaign wizard, generated-ads gallery, video upload, no-browser-dialogs policy, smoke). `npm run test:e2e`; needs `BASE_URL`, `TEST_EMAIL`, `TEST_PASSWORD`. Verified 42/42 against a local stack on this commit. agent-browser smoke scripts remain the default `npm test`.
- Ops scripts under `backend/scripts/`: `copy_prod_to_dev.py` (brands/products/profiles between databases), `cleanup_searches_and_ads.py`, `check_all_ads_dates.py`.
- `.gitignore`: `.env*.local`, `.vercel`.

### Not ported
- Client-specific research specs, the exploratory drag-drop and example specs, a one-off Facebook-pages data migration, and stale test-plan documents. Application code was already fully consolidated; the private repo has no code changes left to bring over.

Plan: none (asset consolidation). Tests: Playwright 42 passing; backend suite unchanged.

## [2026-09-05] Ads Studio integration (PR #3 by masgant99, reworked)

Merged masgant99's PR #3 with their authorship preserved, then fixed every finding from the six-model security review before landing it.

### Added
- Google Ads: OAuth connect, multi-account select, campaign and ad performance, and guarded write actions (create paused, pause, enable, negative keywords) behind `confirm=true` and `campaigns:write`.
- TikTok Ads: OAuth connect, multi-advertiser select, campaign performance, guarded campaign creation.
- Meta: per-user OAuth with multi-account select, long-lived token exchange, and the existing env system token as fallback when a user has no connection.
- Cross-platform Overview page at `/overview` (Dashboard stays the home page).
- Read-only bot API (`/api/v1/bot/*`) with SHA-256 hashed, scope-limited API keys bound to their owner; `backend/scripts/create_api_key.py` mints them.
- OAuth tokens encrypted at rest with a dedicated `OAUTH_TOKEN_ENCRYPTION_KEY` (required; app refuses to boot without it).
- Refresh tokens stored as SHA-256 hashes. Expand step only: `token_hash` added, `token` kept nullable so an in-flight old container keeps working; `backend/alembic/pending/z9_drop_refresh_token_plaintext.py` drops it next release. All existing sessions must log in again.
- White-label branding via `VITE_APP_*` env, defaulting to BreadWinner. Mobile layout pass, ErrorBoundary, token-expiry warning.
- Optional self-host path: `docker-compose.yml`, `frontend/Dockerfile`, `frontend/nginx.conf`.

### Fixed (review findings, all on top of the PR)
- Migration chain: original 16 revisions restored; new tables chained additively onto `add_page_fields_001`; verified on an empty DB, a prod-like stamped DB, and old plaintext refresh rows.
- OAuth callbacks require the browser-bound state cookie (login-CSRF fix); TikTok now clears it.
- Google/TikTok write routes require `campaigns:write`; bot `/connections` scoped to the key owner.
- Facebook SDK objects always built with an explicit `api=` (per-user tokens no longer race on the SDK global).
- Reselecting the already-active ad account no longer deactivates it.
- `TRUSTED_PROXIES` default back to `*` for Railway; backend CSP no longer sent on `/api/v1/docs`.
- TikTok campaigns page loads on mount and on date change; overview handles all eight date presets.
- New ads default to PAUSED (matches media-team feedback #13).

### Removed
- Contributor branding, public legal pages, sprint docs, project brief, and production compose file.

Plan: [.claude/plans/pr3-backend.md](.claude/plans/pr3-backend.md), [.claude/plans/breadwinner-tiktok.md](.claude/plans/breadwinner-tiktok.md); verification evidence in [.claude/plans/pr3-backend-verification.md](.claude/plans/pr3-backend-verification.md). Tests: backend pytest 214 passing, 1 xpassed; frontend vitest 5 passing; Vite build OK.


## [2026-09-03] Fork contributions: masgant99 auth pass

### Security
- 44 API routes had no authentication at all: the whole Research module (saved searches, verticals, blacklists, brand scrapes, run-scheduled-searches), Ad Remix, ad styles, prompts, dashboard stats, copy generation, and file uploads. Verified with the test client returning 200 without a token. Every one now requires a logged-in user; only login and refresh remain public. (masgant99 PR #3, commit `98d033c`, router files only)
- Settings page now sends the bearer token when loading prompts and ad styles. (masgant99 `98d033c`)
- Research API client (`frontend/src/api/research.js`) now attaches the bearer token and refreshes once on 401, matching `authFetch`. Without this the Research pages would have broken once their routes were protected.

Plan: none (external contribution review). Tests: backend pytest 99 passing; unauthenticated probe of 8 previously-open routes now returns 401; frontend `vite build` OK.

## [2026-09-03] Fork contributions: ryuiciwazaka subset

### Added
- `GET /api/v1/facebook/insights`: Marketing API insights (spend, impressions, CTR, ROAS, actions) at account, campaign, ad set, or ad level, by date preset or custom range, optional breakdown. Backend only; groundwork for Reporting and the planned Facebook data cache. (ryuiciwazaka `7caaf39`, backend files)

### Fixed
- Ad Library scraper honors `country` and `active_status` from the saved page URL instead of hardcoding US/active, forces an en-US locale so DOM selectors match in other countries, and waits on `domcontentloaded` with a longer timeout. (ryuiciwazaka `94a3dae`, `6ca1e62`)

### Not taken from this fork
- Turkish-language Reporting page, ad edit/pause/budget drawer, A/B duplicate, creative analytics, and VLM strategy recommender. All are tailored to one Turkish boutique (prompts, currency, power words), the duplicate flow falls back to the contributor's store URL, and the write endpoints use the login check instead of `campaigns:write`. Kept as reference for the reporting work.

Plan: none (external contribution review). Tests: backend pytest 99 passing, 1 xfailed.

## [2026-09-03] Fork contributions: SmittyCode subset

### Added
- `backend/startup.py`: database bootstrap. Empty database gets the schema from the models and is stamped at Alembic head; existing databases run `alembic upgrade head`; a database with tables but no `alembic_version` is refused. Fixes fresh Railway deploys, which failed because the first Alembic revision assumes existing tables. (SmittyCode `aac7b16`)
- `frontend/src/utils/mediaUrl.js`: resolves backend-relative `/uploads/...` media paths against the API origin so legacy generated ads render on split frontend/backend services. (SmittyCode `20ca2bf`)
- `frontend/railway.toml` for the frontend service; root `railway.toml` now configures only the backend with the Dockerfile builder. Closes the cause of issue #1 (Nixpacks at repo root). (SmittyCode `a5e5342`, `94047a6`)

### Fixed
- Batch-saving generated ads no longer fails when the image wizard used a built-in style: style IDs are not `winning_ads` rows, so `template_id` is nulled instead of violating the foreign key. (SmittyCode `20ca2bf`)
- `POST /api/v1/brands` and `POST /api/v1/profiles` no longer 307-redirect to the trailing-slash form. (SmittyCode `2efd969`, `230540c`)
- Vite preview accepts `.up.railway.app` hosts plus any listed in `PREVIEW_ALLOWED_HOSTS`. (SmittyCode `d999fef`, adapted)

### Not taken from this fork
- Removing the brand-to-profile filter in the ad wizards (product decision, not a bug).
- The alternate R2 upload rewrite; main already carries an equivalent fix.
- The contributor's `AGENTS.md`.

Plan: none (external contribution review). Tests: backend pytest 99 passing, 1 xfailed; `startup.py` exercised against empty, migrated, and unversioned local databases; frontend `vite build` OK.

## [2026-09-03]

### Changed
- This repo is now the canonical codebase. Hosted service migration and archival are tracked separately from the canonical repository designation.

### Fixed
- Generated images now upload to Cloudflare R2 instead of local disk, so they survive redeploys (ported from breadWinner `cc921a1`).
- Database engine uses `pool_pre_ping`, `pool_recycle`, and a connect timeout to recover from dropped Railway connections (ported from breadWinner `cc921a1`).
- Batch save endpoint logs full tracebacks on failure (ported from breadWinner `cd71f4b`).
- Backend test suite: disable the slowapi login rate limit under pytest. Fixtures log in per test and were tripping the 5/min limit, failing 77 of 100 tests since the public-release hardening commit.

Plan: none (port of existing fixes). Tests: backend pytest 99 passing, 1 xfailed (local Postgres 15, Python 3.12).
