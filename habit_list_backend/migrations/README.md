# Database migrations

Relational schema changes are managed by Alembic. Production startup validates
the migration state and never calls `create_all`.

```powershell
& '..\.conda\python.exe' -m alembic upgrade head
& '..\.conda\python.exe' -m alembic current
```

SQLite local tests may continue using `DATABASE_SCHEMA_MODE=auto_create`.
