# Phase 16: Async ORM — Write Path

**Part:** ORM
**Depends on:** Phase 15
**Usable after:** yes.

## Tasks

- `await doc.save()` / `insert()` / `delete()` async-primary; sync compat wrappers.
- Controller hooks (`validate`, `on_update`, …) through `dispatch()` — async or sync controllers both work; third-party sync controllers unmodified.
