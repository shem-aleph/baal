# LiberClaw Improvements Master Document

## Tonight's Implementation Plan

Prioritized by: impact × feasibility (can implement and test locally tonight)

### Round 1: Database & API Hardening
- [ ] **DB indexes** — Add missing indexes on frequently queried fields (PERF-001)
- [ ] **Agent name uniqueness** — Add unique constraint per owner (DB-002)
- [ ] **Skills JSON column** — Use PostgreSQL JSON type instead of TEXT (DB-003)
- [ ] **API pagination** — Add cursor-based pagination to agent listing (API-002)
- [ ] **Input validation** — Add field length limits to agent create/update (SEC-010)
- [ ] **Agent filtering/sorting** — Add query params for status, date, sort (API-005)

### Round 2: Auth & Security
- [ ] **Rate limiting** — Add slowapi rate limiting to auth endpoints (SEC-007)
- [ ] **Magic link security** — Use secrets.randbelow() instead of random.randint (SEC-006)
- [ ] **CORS hardening** — Ensure no wildcard with credentials (SEC-009)
- [ ] **Health check depth** — Add DB connectivity check to /health (DEVOPS-001)
- [ ] **Secret validation** — Validate required secrets on startup (config.py)

### Round 3: Code Quality & Testing
- [x] **OAuth dedup** — Extract common OAuth provider logic (QUAL-004)
- [x] **API test suite** — Integration tests for auth, agents, chat endpoints (TEST-001)
- [x] **Error response standardization** — Consistent error format (API-001)
- [x] **Migration rollbacks** — Add downgrade() to all migrations (DB-005) *(already done)*

### Round 4: Features
- [ ] **Agent status websocket/SSE** — Real-time deployment updates
- [ ] **Usage tracking improvements** — Per-agent message counts
- [x] **Agent export/import** — JSON export of agent configs
- [x] **Bulk operations** — Delete multiple agents

### Deferred (needs arch decisions / too risky for overnight)
- SEC-001: Command injection in agent tools (needs sandbox redesign)
- SEC-002: Env var exposure (needs process isolation)
- MISS-002: Agent sharing (needs permission model design)
- DEVOPS-004: Container orchestration (production infrastructure)
- FE-001-004: Frontend changes (need Expo build testing)

---

## Progress Log

### Round 1+2 — Complete
_Completed: 2026-02-15T02:30Z_
DB indexes, validation, pagination, rate limiting, health checks, secret validation.

### Round 3 — Complete
_Completed: 2026-02-15T09:35Z_
- OAuth dedup: extracted `_oauth_redirect`/`_oauth_callback` helpers, reducing ~90 lines of duplication
- Error standardization: all errors now return `{error: {code, message, status, details}}`
- Test fixes: fixed mock paths, status filter, duplicate name test, trailing slash redirect
- Migration rollbacks: all 6 migrations already had downgrade() functions
- 87 tests passing (81 original + 6 new)

### Round 4 — In Progress
_Last updated: 2026-02-15T09:35Z_
- Agent export/import: done (previous commit)
- Bulk delete: POST /agents/bulk-delete with partial success reporting
- Remaining: agent status websocket/SSE, usage tracking improvements
