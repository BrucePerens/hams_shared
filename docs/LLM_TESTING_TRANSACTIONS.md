# Testing & Transaction Isolation in Odoo

When writing tests in this environment, it is critical to understand the distinction between standard HTTP tests (`HamsHttpCase`) and real transactional tests (`RealTransactionCase`). Incorrect usage leads to database pollution, "404 Not Found" errors, and "could not serialize access due to concurrent delete" database deadlocks.

## 1. `HamsHttpCase` (The Default for HTTP / API Tests)

**When to use:**
Use `HamsHttpCase` for **all standard HTTP controllers, website routes, and APIs** that do not explicitly require testing database locking, concurrent deadlocks, or headless Chrome UI tours.

**How it works:**
`HamsHttpCase` (which extends Odoo's standard `HttpCase`) hacks the Werkzeug server thread to share the exact same `TestCursor` as the testing thread. 
- You **do not** and **must not** call `self.env.cr.commit()`.
- Data is safely rolled back when the test completes.

**The Golden Rule for Visibility:**
Because the ORM caches newly created records in memory, Werkzeug will return a `404 Not Found` if you attempt to use `self.url_open()` before pushing the SQL queries to the shared cursor.
You MUST call `self.env.flush_all()` at the end of your `setUp()` method (or right before `self.url_open()`) so the raw SQL `INSERT`s are flushed to the `TestCursor` for Werkzeug to see.

```python
from odoo.addons.zero_sudo.tests.common import HamsHttpCase

class TestMyFeature(HamsHttpCase):
    def setUp(self):
        super().setUp()
        self.user = self.env["res.users"].create({...})
        
        # Pushes pending queries to the TestCursor so Werkzeug can see them
        self.env.flush_all()

    def test_my_route(self):
        # Werkzeug shares the TestCursor, so it can see the uncommitted self.user
        response = self.url_open(f"/{self.user.website_slug}/home")
        self.assertEqual(response.status_code, 200)
```

## 2. `RealTransactionCase` (For Headless Chrome & Concurrency)

**When to use:**
Use `RealTransactionCase` **only** when absolutely necessary:
1. **Headless Chrome UI Tours**: Chrome DevTools Protocol and WebSocket navigation fundamentally deadlock Odoo's `TestCursor`. 
2. **Concurrency / Locking Tests**: Testing `NOWAIT` locks, snapshot isolation caching anomalies (`test_cache_coherence.py`), or background cron race conditions.

**How it works:**
`RealTransactionCase` bypasses Odoo's test framework wraps. It operates on a completely raw, un-mocked PostgreSQL connection. 

**The Danger Zone:**
If another thread (like Headless Chrome or a background task) needs to see data created in your test, you **must explicitly call `self.env.cr.commit()`**.
However, permanently committing data violates the test sandbox. `RealTransactionCase` attempts to clean up its own messes in `tearDown()` by manually issuing raw SQL `DELETE` cascades. 

### Why `RealTransactionCase` Pollutes the Database
If you use `RealTransactionCase` for standard HTTP tests (`url_open`) and explicitly `commit()`, you trigger a deadly race condition:
1. Your test creates `res.users` and calls `commit()`.
2. You run `self.url_open()`. Werkzeug receives the request and creates untracked system records (e.g., `http.session`, `website.visitor`) linked to your test user.
3. Your test `tearDown()` runs immediately after `url_open()` returns. It attempts to `unlink()` the `res.users` record.
4. Werkzeug is still shutting down its connection, holding an active snapshot or lock.
5. PostgreSQL throws `could not serialize access due to concurrent delete`.
6. `tearDown()` fails, abandoning the records permanently ("Database pollution detected!").
7. The next test in the suite tries to create the same user slug and crashes with a "Duplicate key value" error.

**Always default to `HamsHttpCase` with `flush_all()` unless building UI Tours or testing strict database locks.**
