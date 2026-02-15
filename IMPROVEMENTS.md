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
- [ ] **OAuth dedup** — Extract common OAuth provider logic (QUAL-004)
- [ ] **API test suite** — Integration tests for auth, agents, chat endpoints (TEST-001)
- [ ] **Error response standardization** — Consistent error format (API-001)
- [ ] **Migration rollbacks** — Add downgrade() to all migrations (DB-005)

### Round 4: Features
- [ ] **Agent status websocket/SSE** — Real-time deployment updates
- [ ] **Usage tracking improvements** — Per-agent message counts
- [ ] **Agent export/import** — JSON export of agent configs
- [ ] **Bulk operations** — Delete multiple agents

### Deferred (needs arch decisions / too risky for overnight)
- SEC-001: Command injection in agent tools (needs sandbox redesign)
- SEC-002: Env var exposure (needs process isolation)
- MISS-002: Agent sharing (needs permission model design)
- DEVOPS-004: Container orchestration (production infrastructure)
- FE-001-004: Frontend changes (need Expo build testing)

---

## Progress Log

### Round 1 — Starting
_Last updated: 2026-02-15T02:05Z_
