"""The dialect seam — the ONLY layer that knows a specific database exists.

Everything above ``dialects/`` speaks the abstract :class:`~db_conn_mcp.dialects.base.Dialect`
contract. Adding MySQL/SQLite later is one new file here plus one registry line.
"""
