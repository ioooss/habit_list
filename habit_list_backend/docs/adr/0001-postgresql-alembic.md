# ADR 0001：生产数据库采用 PostgreSQL、pgvector 与 Alembic

- 状态：Accepted
- 日期：2026-08-01

## 背景

现有应用以 SQLite `create_all` 起步，适合单机开发，但不足以支撑多进程写入、可靠迁移、锁语义与向量检索的生产需求。Memory V2 又要求 Claim、Evidence、Revision、Tombstone 和 Outbox 在同一个事务边界内保持一致。

## 决策

- 本地继续允许 SQLite + `auto_create`，降低开发成本。
- staging/prod 使用 PostgreSQL 与 `postgresql+psycopg` 异步驱动。
- 所有关系型 schema 变更进入 Alembic；生产启动只验证关键表，不自动建表。
- 向量统一存为 pgvector `VECTOR(1024)`，首版使用 HNSW cosine 索引。
- SQLite FTS5/sqlite-vss 仅作为本地能力，不进入生产迁移。

## 结果

获得明确的 schema 版本、可审查迁移、多 Worker 锁能力和原生向量索引。代价是生产必须运维 PostgreSQL，嵌入维度修改需要专门迁移与重算；首个迁移上线前也必须完成备份和恢复演练。
