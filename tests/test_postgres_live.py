"""PostgresDialect key introspection against a live PostgreSQL server.

The fake connection in test_postgres.py never runs the catalog SQL, so a query that
pairs every column of a composite key with every referenced column passes there. These
tests run that SQL on a real server. Every test is skipped unless ``DB_CONN_MCP_TEST_DSN``
names a database where the role may create a schema; CI's "Live Postgres" job sets it.
A set but unreachable DSN fails the tests rather than skipping them.

Tables are created on a plain ``asyncpg.connect`` connection in a uniquely named schema
(not TEMP tables, which live in ``pg_temp_N``), dropped again after each test.
"""

import os
import uuid

import asyncpg
import pytest

from db_conn_mcp.dialects.postgres import PostgresDialect

pytestmark = pytest.mark.skipif(
    "DB_CONN_MCP_TEST_DSN" not in os.environ, reason="DB_CONN_MCP_TEST_DSN not set"
)

# {schema} is a generated name of letters, digits and underscores, safe to interpolate.
# transfers declares its constraints in an order that differs from both the column order
# and the constraint-name order, so only "by name, then by declared position" passes.
_CREATE_TABLES_SQL = """
    CREATE TABLE {schema}.orgs (
        id integer,
        tenant text,
        PRIMARY KEY (id, tenant)
    );
    CREATE TABLE {schema}.users (
        org_id integer,
        tenant text,
        PRIMARY KEY (org_id, tenant),
        CONSTRAINT users_org_fkey FOREIGN KEY (org_id, tenant)
            REFERENCES {schema}.orgs (id, tenant)
    );
    CREATE TABLE {schema}.transfers (
        id integer,
        seq integer,
        from_org integer,
        from_tenant text,
        to_org integer,
        to_tenant text,
        PRIMARY KEY (seq, id),
        CONSTRAINT transfers_to_fkey FOREIGN KEY (to_org, to_tenant)
            REFERENCES {schema}.users (org_id, tenant),
        CONSTRAINT transfers_from_fkey FOREIGN KEY (from_tenant, from_org)
            REFERENCES {schema}.orgs (tenant, id)
    );
"""

# Same users as above, but the table it references is partitioned: Postgres adds one extra
# foreign-key row per referenced partition, which must not show up as extra entries.
_CREATE_PARTITIONED_TABLES_SQL = """
    CREATE TABLE {schema}.orgs (
        id integer,
        tenant text,
        PRIMARY KEY (id, tenant)
    ) PARTITION BY LIST (tenant);
    CREATE TABLE {schema}.orgs_p1 PARTITION OF {schema}.orgs FOR VALUES IN ('a');
    CREATE TABLE {schema}.orgs_p2 PARTITION OF {schema}.orgs FOR VALUES IN ('b');
    CREATE TABLE {schema}.users (
        org_id integer,
        tenant text,
        PRIMARY KEY (org_id, tenant),
        CONSTRAINT users_org_fkey FOREIGN KEY (org_id, tenant)
            REFERENCES {schema}.orgs (id, tenant)
    );
"""

USERS_PRIMARY_KEY = ["org_id", "tenant"]
USERS_FOREIGN_KEYS = [
    {"column": "org_id", "references": "orgs.id"},
    {"column": "tenant", "references": "orgs.tenant"},
]
TRANSFERS_PRIMARY_KEY = ["seq", "id"]
TRANSFERS_FOREIGN_KEYS = [
    {"column": "from_tenant", "references": "orgs.tenant"},
    {"column": "from_org", "references": "orgs.id"},
    {"column": "to_org", "references": "users.org_id"},
    {"column": "to_tenant", "references": "users.tenant"},
]


@pytest.fixture
async def conn_and_schema():
    """Yield a plain connection and the name of a fresh schema holding the test tables."""
    conn = await asyncpg.connect(os.environ["DB_CONN_MCP_TEST_DSN"])
    schema = f"keys_{uuid.uuid4().hex}"
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(_CREATE_TABLES_SQL.format(schema=schema))
        yield conn, schema
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


@pytest.fixture
async def conn_and_partitioned_schema():
    """Like conn_and_schema, with the partitioned orgs and its users."""
    conn = await asyncpg.connect(os.environ["DB_CONN_MCP_TEST_DSN"])
    schema = f"keys_{uuid.uuid4().hex}"
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(_CREATE_PARTITIONED_TABLES_SQL.format(schema=schema))
        yield conn, schema
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


def _database_entry(database_schema: dict, schema: str, table: str) -> dict:
    return next(
        t for t in database_schema["tables"] if t["schema"] == schema and t["name"] == table
    )


async def test_get_schema_lists_each_composite_key_column_once(conn_and_schema):
    conn, schema = conn_and_schema
    result = await PostgresDialect().get_schema(conn, f"{schema}.users")
    assert result["primary_key"] == USERS_PRIMARY_KEY
    assert result["foreign_keys"] == USERS_FOREIGN_KEYS


async def test_get_database_schema_lists_each_composite_key_column_once(conn_and_schema):
    conn, schema = conn_and_schema
    users = _database_entry(await PostgresDialect().get_database_schema(conn), schema, "users")
    assert users["primary_key"] == USERS_PRIMARY_KEY
    assert users["foreign_keys"] == USERS_FOREIGN_KEYS


async def test_two_composite_foreign_keys_are_contiguous_and_in_declared_order(conn_and_schema):
    conn, schema = conn_and_schema
    dialect = PostgresDialect()
    single = await dialect.get_schema(conn, f"{schema}.transfers")
    assert single["primary_key"] == TRANSFERS_PRIMARY_KEY
    assert single["foreign_keys"] == TRANSFERS_FOREIGN_KEYS

    transfers = _database_entry(await dialect.get_database_schema(conn), schema, "transfers")
    assert transfers["primary_key"] == TRANSFERS_PRIMARY_KEY
    assert transfers["foreign_keys"] == TRANSFERS_FOREIGN_KEYS


async def test_foreign_key_into_partitioned_table_lists_each_column_once(
    conn_and_partitioned_schema,
):
    conn, schema = conn_and_partitioned_schema
    dialect = PostgresDialect()
    single = await dialect.get_schema(conn, f"{schema}.users")
    assert single["primary_key"] == USERS_PRIMARY_KEY
    assert single["foreign_keys"] == USERS_FOREIGN_KEYS

    database_schema = await dialect.get_database_schema(conn)
    users = _database_entry(database_schema, schema, "users")
    assert users["primary_key"] == USERS_PRIMARY_KEY
    assert users["foreign_keys"] == USERS_FOREIGN_KEYS

    # Each partition keeps its own cloned primary key and has no foreign key.
    for partition in ("orgs_p1", "orgs_p2"):
        entry = _database_entry(database_schema, schema, partition)
        assert entry["primary_key"] == ["id", "tenant"]
        assert entry["foreign_keys"] == []
        single_partition = await dialect.get_schema(conn, f"{schema}.{partition}")
        assert single_partition["primary_key"] == ["id", "tenant"]
        assert single_partition["foreign_keys"] == []


async def test_get_schema_missing_table_raises_not_found(conn_and_schema):
    """pg_class exists in pg_catalog only, so qualifying it with this schema must miss."""
    conn, schema = conn_and_schema
    missing = f"missing_{uuid.uuid4().hex}"
    for table in (missing, f"{schema}.{missing}", f"{schema}.pg_class"):
        with pytest.raises(ValueError) as error:
            await PostgresDialect().get_schema(conn, table)
        assert str(error.value) == (
            f"Table {table!r} was not found. Use list_tables to see the tables in this database."
        )


async def test_get_schema_zero_column_table_is_found(conn_and_schema):
    conn, schema = conn_and_schema
    await conn.execute(f"CREATE TABLE {schema}.empty ()")
    result = await PostgresDialect().get_schema(conn, f"{schema}.empty")
    assert result["columns"] == []


async def test_get_schema_view_and_materialized_view_are_found(conn_and_schema):
    """information_schema.columns leaves out a materialized view's columns; it still exists."""
    conn, schema = conn_and_schema
    await conn.execute(f"CREATE VIEW {schema}.orgs_view AS SELECT * FROM {schema}.orgs")
    await conn.execute(
        f"CREATE MATERIALIZED VIEW {schema}.orgs_matview AS SELECT * FROM {schema}.orgs"
    )
    dialect = PostgresDialect()
    view = await dialect.get_schema(conn, f"{schema}.orgs_view")
    assert [column["name"] for column in view["columns"]] == ["id", "tenant"]
    matview = await dialect.get_schema(conn, f"{schema}.orgs_matview")
    assert matview["table"] == f"{schema}.orgs_matview"


async def test_list_tables_and_find_columns_rows_carry_the_documented_keys(conn_and_schema):
    conn, schema = conn_and_schema
    dialect = PostgresDialect()
    tables = [row for row in await dialect.list_tables(conn) if row["schema"] == schema]
    assert set(tables[0]) == {"schema", "name", "kind"}
    columns = [row for row in await dialect.find_columns(conn, "tenant") if row["schema"] == schema]
    assert set(columns[0]) == {"schema", "table", "column", "type", "nullable"}


@pytest.fixture
async def conn_and_listing():
    """Yield a connection, a unique tag and the rows list_tables must return for it.

    Two schemas, so the order is checked across schemas as well as within one: the
    first holds orders<tag> and the view archive<tag> made after it, so creation order
    is not name order; the second holds accounts<tag>. Every table name carries the
    tag, so filtering on it isolates these rows from anything else on the server.
    """
    conn = await asyncpg.connect(os.environ["DB_CONN_MCP_TEST_DSN"])
    tag = uuid.uuid4().hex
    first, second = f"tables_a_{tag}", f"tables_b_{tag}"
    try:
        await conn.execute(f"CREATE SCHEMA {first}")
        await conn.execute(f"CREATE SCHEMA {second}")
        await conn.execute(f"CREATE TABLE {first}.orders{tag} (id integer)")
        await conn.execute(
            f"CREATE VIEW {first}.archive{tag} AS SELECT id FROM {first}.orders{tag}"
        )
        await conn.execute(f"CREATE TABLE {second}.accounts{tag} (id integer)")
        expected = [
            {"schema": first, "name": f"archive{tag}", "kind": "view"},
            {"schema": first, "name": f"orders{tag}", "kind": "table"},
            {"schema": second, "name": f"accounts{tag}", "kind": "table"},
        ]
        yield conn, tag, expected
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {first} CASCADE")
        await conn.execute(f"DROP SCHEMA IF EXISTS {second} CASCADE")
        await conn.close()


async def test_list_tables_orders_by_schema_then_name_and_skips_system_schemas(
    conn_and_listing,
):
    conn, tag, expected = conn_and_listing
    rows = await PostgresDialect().list_tables(conn)
    assert [row for row in rows if row["name"].endswith(tag)] == expected
    assert not [row for row in rows if row["schema"] in ("pg_catalog", "information_schema")]


async def test_list_tables_pattern_is_a_case_insensitive_containment_match(conn_and_listing):
    conn, tag, expected = conn_and_listing
    dialect = PostgresDialect()
    assert await dialect.list_tables(conn, pattern=tag) == expected
    assert await dialect.list_tables(conn, pattern=f"RDERS{tag.upper()}") == expected[1:2]


async def test_list_tables_limit_keeps_the_first_rows_in_order(conn_and_listing):
    conn, tag, expected = conn_and_listing
    assert await PostgresDialect().list_tables(conn, pattern=tag, limit=2) == expected[:2]
