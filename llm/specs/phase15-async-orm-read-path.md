# Phase 15: Async ORM — Read Path

**Part:** ORM (2700+ lines — split read / write / lifecycle)
**Depends on:** Phase 6
**Usable after:** yes.

## Tasks

- `await frappe.get_doc(...)` is the default; sync `frappe.get_doc(...)` keeps working via `async_to_sync` facade.
- `frappe.get_all` / `get_list` / `get_value` on Document level, same dual shape.
- Sync callers in custom apps work unchanged — port at their own pace.
- **Memory — bound the meta cache**: Frappe's DocType meta cache is duplicated across every worker today; single-process = one copy (free win). Push further — **LRU-bound** the in-process meta cache so a 1000-DocType site doesn't pin every schema in RSS forever.
