# Validation — Hardcore Economy 2.1

Validated 2026-10-17 against the supplied DCSServerBot source snapshot and Hardcore
Economy 2.0 archive. No live Discord messages, DCS actions, or production database
writes were performed.

- **38 automated tests passed**, including actual Lua execution with mocked DCS
  objects and production economy SQL executed in PostgreSQL WASM (PGlite 0.5.10).
- Python files compile; all three Lua files parse successfully.
- Upgrade from the old schema preserves existing credits, membership and aircraft
  time. Restart reconciliation excludes downtime.
- First ejection retains Hardcore and deducts aircraft payout; second ejection
  after a new session revokes it. Direct death revokes immediately.
- Protected ground incidents do not count. Duplicate crash/death/ejection messages
  do not double-charge. New aircraft incarnations have distinct incident identity.
- Friendly/neutral/enemy/off-field, captured base, FARP radius, carrier deck/sea,
  missing landing callback, delayed old-aircraft events and multicrew scenarios
  are covered by Lua fixtures.
- Support has no Hardcore bonus. Role switching, independent block totals, credit
  caps, fractional carry and duplicate settlement behavior are covered.
- AwardAutomation compatibility patch was applied twice to a temporary copy of
  the supplied v1.6 file; it is idempotent and leaves the existing repository
  unchanged. Its query excludes support-only medal progress.

The test runner reports one upstream discord.py `audioop` deprecation warning.
There are no test failures. Live DCS geometry/module acceptance checks remain;
see INSTALL.md. Tests cannot substitute for validating actual server event delivery.
