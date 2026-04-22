This folder stores the local **SQLite 3** database file used by the backend.

Python’s `sqlite3` module and the `aiosqlite` driver both use the **SQLite 3** library (file format SQLite 3.x). There is no legacy SQLite 2 stack in supported Python versions.

- Default path: `backend/db/app.db`
- Managed with Alembic migrations.
- Safe to delete in development if you want a clean database (then run migrations again).

To see the exact engine version at runtime: `import sqlite3; print(sqlite3.sqlite_version)` (also logged once when the API starts if `DATABASE_URL` is a `sqlite` URL).
