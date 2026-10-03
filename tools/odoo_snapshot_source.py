#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""odoo_snapshot_source: read an OFFLINE Odoo snapshot (a restored PostgreSQL dump plus the filestore
directory) through the same five read methods the exporter uses on a live server (ADR 0105).

    SnapshotSource(db, filestore).call(model, "search_read" | "read" | "fields_get" | "search_count" | "search", ...)

The exporter in odoo_site_migrate.py talks to a "source" object that has `version()` and
`call(model, method, args, kwargs)`. A live Odoo implements that over XML-RPC; this module implements
it over SQL, so `export --snapshot-db NAME --filestore DIR` produces exactly the export format the
importer reads, with no Odoo of the source's version running anywhere.

Properties:

- Read only: the connection is opened read-only, and only SELECT statements are built.
- Model and field metadata come from the snapshot's own `ir_model_fields` (the same table
  `fields_get` is answered from on a live server), so an Odoo 16, 17 or 18 snapshot works.
- Translated fields (jsonb in Odoo 16+) are answered in `en_US` (or the first language present),
  as XML-RPC answers in the user's language.
- Binary fields stored as attachments (`res_field`) and `ir.attachment.datas` come from the filestore
  directory (`<root>/ab/abcdef...`) or `db_datas`.
- Identifiers are never taken from a caller: a table or column must exist in the snapshot's schema.
- The schema access goes through a tiny `Db` interface (`PgDb` here; the tests use SQLite), so no part
  of the logic needs a PostgreSQL server to be tested.
"""

import base64
import datetime
import decimal
import json
import os
import re
import subprocess
import unicodedata
import xmlrpc.client

LANG = "en_US"
IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
ORDER_RE = re.compile(r"^([a-z_][a-z0-9_]*)(?:\s+(asc|desc))?$", re.I)
SNAPSHOT_DB_PREFIX = "mig_"  # restore refuses any other database name: it must never touch a real one


class SnapshotError(RuntimeError):
    pass


def _fault(message):
    return xmlrpc.client.Fault(1, message)


# --------------------------------------------------------------------------------------------
# Database access
# --------------------------------------------------------------------------------------------


class PgDb:
    """PostgreSQL through psycopg2, read-only. `dsn` is a libpq string such as "dbname=mig_x"."""

    placeholder = "%s"

    def __init__(self, dsn, connect=None):
        if connect is None:
            import psycopg2  # imported late: only the snapshot commands need it

            connect = psycopg2.connect
        self.conn = connect(dsn)
        self.conn.set_session(readonly=True, autocommit=True)

    def query(self, sql, params=()):
        with self.conn.cursor() as cursor:
            cursor.execute(sql, params)
            names = [d[0] for d in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

    def columns(self, table):
        rows = self.query(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s",
            (table,),
        )
        return {row["column_name"] for row in rows}

    def close(self):
        self.conn.close()


# --------------------------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------------------------


def pick_language(value):
    """A translated jsonb value as one string: en_US, else the first language present."""
    if isinstance(value, str) and value[:1] == "{":
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if isinstance(value, dict):
        if value.get(LANG) is not None:
            return value[LANG]
        for item in value.values():
            if item is not None:
                return item
        return False
    return value


def _slugify(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[\W_]+", "-", text).strip("-").lower()


def filestore_path(root, store_fname):
    """<root>/<store_fname>, accepting a root that is the filestore itself or its parent (one database
    directory below it). Refuses names that would leave the root."""
    if not store_fname or ".." in store_fname.split("/") or store_fname.startswith("/"):
        raise SnapshotError(f"unsafe store_fname {store_fname!r}")
    direct = os.path.join(root, store_fname)
    if os.path.exists(direct):
        return direct
    try:
        children = sorted(os.listdir(root))
    except OSError:
        return direct
    for child in children:
        candidate = os.path.join(root, child, store_fname)
        if len(child) > 2 and os.path.exists(candidate):
            return candidate
    return direct


# --------------------------------------------------------------------------------------------
# The source
# --------------------------------------------------------------------------------------------


# [@ANCHOR: odoo_snapshot_source:read_only]
# Verified by [@ANCHOR: test_odoo_snapshot_source:read_only]
class SnapshotSource:
    """Implements version() and call() over a snapshot database. Only read methods exist."""

    METHODS = ("search", "search_read", "read", "fields_get", "search_count")

    def __init__(self, db, filestore=None):
        self.db = db
        self.filestore = filestore
        self._columns = {}
        self._meta = {}
        self._names = {}
        self.requests = 0  # kept so callers can report "queries" in place of "requests"

    # -- schema
    def table(self, model):
        table = model.replace(".", "_")
        if not IDENT_RE.match(table):
            raise _fault(f"invalid model name {model!r}")
        return table

    def columns(self, table):
        if table not in self._columns:
            self._columns[table] = self.db.columns(table)
        return self._columns[table]

    def has_table(self, model):
        return bool(self.columns(self.table(model)))

    def meta(self, model):
        """{field: ir_model_fields row} for a model."""
        if model not in self._meta:
            rows = self.db.query(
                "SELECT name, ttype, relation, store, translate, relation_table, column1, column2, field_description "
                f"FROM ir_model_fields WHERE model = {self.db.placeholder}",
                (model,),
            )
            self._meta[model] = {row["name"]: row for row in rows}
        return self._meta[model]

    def version(self):
        rows = self.db.query("SELECT latest_version FROM ir_module_module WHERE name = 'base'")
        text = (rows[0]["latest_version"] if rows else "") or "0.0"
        parts = [int(p) for p in re.findall(r"\d+", text)[:2]] or [0, 0]
        major, minor = (parts + [0, 0])[:2]
        return {"server_version": f"{major}.{minor}", "server_version_info": [major, minor, 0, "final", 0, ""],
                "snapshot": True}

    # -- the five methods
    def call(self, model, method, args, kwargs=None):
        kwargs = kwargs or {}
        self.requests += 1
        if method not in self.METHODS:
            raise PermissionError(f"snapshot source cannot {method!r}")
        if method == "fields_get":
            return self.fields_get(model)
        if not self.has_table(model):
            if method in ("search_count",):
                return 0
            if method in ("search", "search_read", "read"):
                raise _fault(f"model {model} has no table in the snapshot (abstract or not installed)")
        if method == "search_count":
            where, params = self.where(model, args[0] if args else [], kwargs)
            return self.db.query(f'SELECT COUNT(*) AS n FROM "{self.table(model)}"{where}', params)[0]["n"]
        if method == "search":
            return [row["id"] for row in self.select(model, args[0] if args else [], kwargs, ["id"])]
        if method == "search_read":
            rows = self.select(model, args[0] if args else [], kwargs, None)
            return [self.output(model, row, kwargs.get("fields")) for row in rows]
        if method == "read":
            ids = list(args[0])
            if not ids:
                return []
            marks = ", ".join([self.db.placeholder] * len(ids))
            rows = self.db.query(f'SELECT * FROM "{self.table(model)}" WHERE id IN ({marks}) ORDER BY id', tuple(ids))
            return [self.output(model, row, kwargs.get("fields")) for row in rows]
        raise AssertionError(method)

    def fields_get(self, model):
        result = {}
        for name, row in self.meta(model).items():
            result[name] = {
                "type": row["ttype"], "relation": row["relation"] or False, "store": bool(row["store"]),
                "string": pick_language(row["field_description"]) or name, "selection": [],
                "translate": bool(row["translate"]),
            }
        return result

    # -- domains
    def where(self, model, domain, kwargs):
        table = self.table(model)
        columns = self.columns(table)
        terms = self._tree(list(domain or []))
        context = kwargs.get("context") or {}
        mentions_active = "active" in str(domain)
        params = []
        clauses = [self._sql(model, columns, node, params) for node in terms]
        if "active" in columns and context.get("active_test") is not False and not mentions_active:
            clauses.append('"active" IS TRUE')
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", tuple(params)

    def _tree(self, terms):
        nodes = []

        def parse():
            if not terms:
                raise _fault("malformed domain")
            term = terms.pop(0)
            if term in ("&", "|"):
                return (term, parse(), parse())
            if term == "!":
                return ("!", parse())
            if not isinstance(term, (list, tuple)) or len(term) != 3:
                raise _fault(f"unsupported domain term {term!r}")
            return ("t", tuple(term))

        while terms:
            nodes.append(parse())
        return nodes

    def _sql(self, model, columns, node, params):
        kind = node[0]
        if kind in ("&", "|"):
            joiner = " AND " if kind == "&" else " OR "
            return "(" + self._sql(model, columns, node[1], params) + joiner + self._sql(model, columns, node[2], params) + ")"
        if kind == "!":
            # Odoo's negation keeps the rows where the field is empty (NULL): NOT NULL would drop them.
            return "COALESCE(NOT " + self._sql(model, columns, node[1], params) + ", TRUE)"
        field, operator, value = node[1]
        if field not in columns or not IDENT_RE.match(field):
            raise _fault(f"the snapshot cannot filter {model} on {field!r} (not a stored column)")
        meta = self.meta(model).get(field)
        if meta and meta["translate"]:
            raise _fault(f"the snapshot cannot filter {model} on the translated field {field!r}")
        ph = self.db.placeholder
        column = f'"{field}"'
        if operator in ("=", "!="):
            if value is False or value is None:
                if self._is_bool(meta):
                    return f"({column} IS NULL OR {column} = FALSE)" if operator == "=" else f"{column} IS TRUE"
                return f"{column} IS NULL" if operator == "=" else f"{column} IS NOT NULL"
            params.append(value)
            if operator == "!=":
                return f"({column} <> {ph} OR {column} IS NULL)"
            return f"{column} = {ph}"
        if operator in ("in", "not in"):
            values = list(value)
            if not values:
                return "1=0" if operator == "in" else "1=1"
            params.extend(values)
            marks = ", ".join([ph] * len(values))
            if operator == "not in":
                return f"({column} NOT IN ({marks}) OR {column} IS NULL)"
            return f"{column} IN ({marks})"
        if operator in (">", "<", ">=", "<="):
            params.append(value)
            return f"{column} {operator} {ph}"
        if operator in ("like", "ilike", "=like", "=ilike"):
            params.append(value if operator.startswith("=") else f"%{value}%")
            if "i" in operator:
                return f"LOWER({column}) LIKE LOWER({ph})"
            return f"{column} LIKE {ph}"
        raise _fault(f"unsupported domain operator {operator!r}")

    @staticmethod
    def _is_bool(meta):
        return bool(meta) and meta["ttype"] == "boolean"

    def select(self, model, domain, kwargs, only):
        table = self.table(model)
        where, params = self.where(model, domain, kwargs)
        order = "id"
        match = ORDER_RE.match(kwargs.get("order") or "id")
        if match and match.group(1) in self.columns(table):
            order = f'"{match.group(1)}" {(match.group(2) or "asc").upper()}'
        sql = f'SELECT * FROM "{table}"{where} ORDER BY {order}'
        if kwargs.get("limit"):
            sql += f" LIMIT {int(kwargs['limit'])}"
        if kwargs.get("offset"):
            sql += f" OFFSET {int(kwargs['offset'])}"
        return self.db.query(sql, params)

    # -- output
    def output(self, model, row, wanted):
        meta = self.meta(model)
        names = list(wanted) if wanted else [n for n, m in meta.items() if m["store"] and n in row]
        out = {"id": row["id"]}
        for name in names:
            if name == "id":
                continue
            out[name] = self.value(model, row, name, meta.get(name))
        return out

    def value(self, model, row, name, meta):
        if model == "ir.attachment" and name == "datas":
            return self.attachment_bytes_b64(row)
        if meta is None:
            return self.plain(row[name]) if name in row and row[name] is not None else self.computed(model, row, name)
        kind = meta["ttype"]
        if kind == "many2many":  # no column: a relation table
            return self.m2m(meta, row["id"])
        if kind == "binary":
            if name in row:
                raw = row[name]
                return base64.b64encode(bytes(raw)).decode() if raw else False
            return self.attached_binary(model, row["id"], name)
        if not meta["store"] or name not in row:
            return self.computed(model, row, name)
        raw = row[name]
        if raw is None:
            return False
        if kind == "boolean":
            return bool(raw)
        if kind == "many2one":
            return [raw, self.display_name(meta["relation"], raw)]
        if kind in ("many2many",):
            return self.m2m(meta, row["id"])
        if kind in ("one2many",):
            return []
        if meta["translate"]:
            return pick_language(raw)
        if kind in ("char", "text", "html", "selection") and isinstance(raw, str):
            return raw
        return self.plain(raw)

    @staticmethod
    def plain(raw):
        """A value XML-RPC could carry: datetimes as Odoo's own strings, decimals as floats."""
        if isinstance(raw, memoryview):
            return base64.b64encode(bytes(raw)).decode()
        if isinstance(raw, datetime.datetime):
            return raw.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(raw, datetime.date):
            return raw.isoformat()
        if isinstance(raw, decimal.Decimal):
            return float(raw)
        return raw

    def computed(self, model, row, name):
        """The few non-stored values the exporter asks for."""
        if name == "website_url" and model == "blog.blog":
            return f"/blog/{_slugify(pick_language(row.get('name')))}-{row['id']}"
        if name == "website_url" and model == "blog.post":
            blog = self.db.query(
                f'SELECT id, name FROM "blog_blog" WHERE id = {self.db.placeholder}', (row.get("blog_id"),)
            )
            if not blog:
                return False
            base = f"/blog/{_slugify(pick_language(blog[0]['name']))}-{blog[0]['id']}"
            return f"{base}/{_slugify(pick_language(row.get('name')))}-{row['id']}"
        return False

    def display_name(self, relation, record_id):
        key = (relation, record_id)
        if key not in self._names:
            name = ""
            if relation and IDENT_RE.match(relation.replace(".", "_")):
                table = relation.replace(".", "_")
                columns = self.columns(table)
                for candidate in ("name", "complete_name", "code", "login"):
                    if candidate in columns:
                        rows = self.db.query(
                            f'SELECT "{candidate}" AS v FROM "{table}" WHERE id = {self.db.placeholder}', (record_id,)
                        )
                        if rows:
                            name = pick_language(rows[0]["v"]) or ""
                        break
            self._names[key] = name if isinstance(name, str) else str(name)
        return self._names[key]

    def m2m(self, meta, record_id):
        table, col1, col2 = meta["relation_table"], meta["column1"], meta["column2"]
        if not (table and col1 and col2 and all(IDENT_RE.match(x) for x in (table, col1, col2))):
            return []
        if not self.columns(table):
            return []
        rows = self.db.query(
            f'SELECT "{col2}" AS other FROM "{table}" WHERE "{col1}" = {self.db.placeholder} ORDER BY "{col2}"', (record_id,)
        )
        return [row["other"] for row in rows]

    # -- files
    def read_stored(self, row):
        fname = row.get("store_fname")
        if fname:
            if not self.filestore:
                raise SnapshotError("a filestore directory is needed for attachments (--filestore)")
            path = filestore_path(self.filestore, fname)
            try:
                with open(path, "rb") as handle:  # audit-ignore-path
                    return handle.read()
            except OSError as exc:
                raise SnapshotError(f"attachment {row.get('id')}: filestore file {fname} missing ({exc})") from exc
        data = row.get("db_datas")
        return bytes(data) if data else b""

    def attachment_bytes_b64(self, row):
        if row.get("type") == "url":
            return False
        raw = self.read_stored(row)
        return base64.b64encode(raw).decode() if raw else False

    def attached_binary(self, model, record_id, field):
        ph = self.db.placeholder
        rows = self.db.query(
            f"SELECT * FROM ir_attachment WHERE res_model = {ph} AND res_id = {ph} AND res_field = {ph} ORDER BY id DESC",
            (model, record_id, field),
        )
        if not rows:
            return False
        return self.attachment_bytes_b64(rows[0])


# --------------------------------------------------------------------------------------------
# Restoring a dump into a scratch database (dev box only)
# --------------------------------------------------------------------------------------------


def restore_snapshot(dump_path, db_name, runner=subprocess.run, exists=os.path.exists):
    """createdb + pg_restore of a custom-format dump into a NEW scratch database named mig_*.

    Refuses any other name, an existing database, and a host that looks like production."""
    if not db_name.startswith(SNAPSHOT_DB_PREFIX) or not IDENT_RE.match(db_name):
        raise SnapshotError(f"scratch database names must look like {SNAPSHOT_DB_PREFIX}<name>: {db_name!r}")
    if exists("/opt/hams/src/DEPLOY_LOG") or exists("/opt/hams/src/DEPLOYED_COMMITS"):
        raise SnapshotError("this looks like the production host; snapshots are restored on the dev box only")
    if not exists(dump_path):
        raise SnapshotError(f"no such dump: {dump_path}")
    listing = runner(["psql", "-At", "-d", "postgres", "-c", f"SELECT 1 FROM pg_database WHERE datname = '{db_name}'"],
                     capture_output=True, text=True, check=True)
    if listing.stdout.strip():
        raise SnapshotError(f"database {db_name} already exists; drop it yourself or pick another name")
    runner(["createdb", db_name], check=True)
    runner(["pg_restore", "--no-owner", "--no-acl", "-d", db_name, dump_path], check=False)
    return db_name
