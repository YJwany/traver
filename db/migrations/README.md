# Database migrations

Alembic 管理数据库结构变更。迁移脚本位于 `db/migrations/versions/`，配置在仓库根目录的 `alembic.ini`
（`script_location = db/migrations`、`prepend_sys_path = .`）。

当前迁移链（单头，`upgrade head` 会依次应用）：

| 顺序 | revision | 内容 |
|---|---|---|
| 1 | `591c65cd5fef` | 初始旅行持久化结构（Trip、快照、来源、预算、路线、决策） |
| 2 | `8b9f3cc905a1` | 报价的商业条款字段 |
| 3 | `c3a1d2e4f607` | AI 会话 |

## 用法

数据库 URL 优先取环境变量 `DATABASE_URL`，未设置时回退到 `alembic.ini` 中的
`sqlalchemy.url`（默认 `sqlite:///./travel_alembic.db`）。因此本地与生产可以共用同一套迁移：

```powershell
# 本地 SQLite
$env:DATABASE_URL='sqlite:///./travel_alembic.db'
alembic upgrade head

# PostgreSQL
$env:DATABASE_URL='postgresql+psycopg://travel:travel@localhost:5432/travel'
alembic upgrade head
```

回退一级或全部回退：

```powershell
alembic downgrade -1
alembic downgrade base
```

查看当前版本 / 历史：

```powershell
alembic current
alembic history --verbose
```

新增迁移（改完 `travel_api/db/models.py` 后）：

```powershell
$env:DATABASE_URL='sqlite:///./travel_alembic.db'
alembic revision --autogenerate -m "描述"
```

`env.py` 使用 `Base.metadata` 作为 `target_metadata` 并开启 `compare_type=True`；对 SQLite 会自动
使用 `render_as_batch=True`，因为 SQLite 的 `ALTER TABLE` 能力有限，批处理模式会以"建新表 + 拷数据"
的方式完成修改。

## 与 AUTO_CREATE_SCHEMA 的关系

`AUTO_CREATE_SCHEMA`（默认 `true`）会在 API 启动时用 SQLAlchemy metadata 直接建表，便于本地零配置
起步。**它与 Alembic 是两条独立的建表路径**：

- 本地开发：可以用 `AUTO_CREATE_SCHEMA=true` 直接起步。
- 生产：应设置 `AUTO_CREATE_SCHEMA=false`，只通过 `alembic upgrade head` 管理结构，避免两套机制
  对同一个库做出不一致的假设。

无论哪种方式，`alembic upgrade head` 都必须是可用的：仓库中有集成测试（`tests/integration/test_migrations.py`）
在 SQLite 上真实执行 `upgrade` 与 `downgrade`，所以迁移脚本不会悄悄失效。
