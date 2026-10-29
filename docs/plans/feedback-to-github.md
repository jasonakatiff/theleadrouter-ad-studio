# In-app feedback → private project board

## Problem

`POST /api/v1/telemetry/feedback` writes one `telemetry_events` row and nothing else.
No one is notified, the only way to read it is the admin Telemetry page, and feedback
inherits telemetry retention (14 days, plus volume-based pruning that deletes oldest
rows regardless of kind). Valentin Volcinschi's 2026-10-28 request was the only
surviving submission and would have been deleted around 2026-11-11.

## Decisions

1. **Destination: a draft item on the private project board**, not a public issue.
   Feedback is untrusted free text and this team's submissions routinely carry client
   ad-account names/IDs and campaign names — the reason issues #5–#29 were sanitised by
   hand. Drafts are private; a human promotes one to a sanitised public issue.
2. **Store at submit time, mirror in the worker.** The endpoint writes a durable
   `feedback_submissions` row in the same transaction as the telemetry event, so
   feedback stops expiring on every install whether or not GitHub is configured. The
   sync worker drains pending rows, so a buyer never waits on GitHub.
3. **Optional and off by default.** This is a public install template; other installs
   are not Jason's. No project id is hardcoded anywhere, including docs examples.

## Shape

- `feedback_submissions`: copy of message/category/page/user/trace plus `status`,
  `attempts`, `next_attempt_at`, `last_error`, `ticket_id`, `sent_at`.
- Migration chains from `public_cutover_20260912` (verified as the single head and as
  production's current revision) and backfills existing `kind='feedback'` telemetry rows.
- `app/services/feedback_mirror.py` owns claim/build/post. The HTTP call is injectable
  so tests drive the boundary; the real client is exercised live once before shipping.
- Worker step wrapped in its own try/except: the loop re-raises, and one GitHub outage
  must not kill metadata sync.

## Risks

- `schema_ready()` compares the alembic revision for **equality** against
  `REQUIRED_SCHEMA_REVISION`, so the new revision must be bumped in `installation.py`,
  `scripts/workspace_preflight.py` and three test assertions — and a rollback to the
  previous image would report the schema unready until the migration is downgraded.
- Credential: GitHub documents classic PAT `project` scope or a GitHub App for
  Projects v2; fine-grained PATs are not documented for user-owned projects. A classic
  PAT reaches every project on the account, so a dedicated bot account added only to
  this board is the least-privilege option. It also lands in the Railway env store that
  was breached on 2026-10-21 and has not been rotated.
- Two workers can overlap during a Railway deploy, so rows are claimed
  `FOR UPDATE SKIP LOCKED`.

## Verification

Unit tests at the HTTP seam, then one live draft created against the real board before
merge. Anything not live-verified is labelled as such.
