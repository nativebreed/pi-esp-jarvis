"""Databases Jarvis can answer questions from: SQLite, MySQL, PostgreSQL and JSON files.

You add a connection and its questions on the Library page. Each question is a phrase ("how many orders
today") and a read-only SELECT. A phrase can have a {placeholder}: "phone number for {name}" runs
`... WHERE name LIKE :name` with whatever followed "phone number for".

Safety: only one SELECT (or WITH ... SELECT) per question, read-only sessions, a time limit and a row
limit. Results go to the local model only; they never reach Claude. Use a read-only database user anyway.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config, privacy

log = logging.getLogger("databases")

KINDS = ("sqlite", "mysql", "postgres", "json")
MAX_ROWS = 50
TIMEOUT_S = 8
_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|attach|detach|pragma|grant|"
                        r"revoke|copy|merge|call|exec|execute|vacuum|reindex|lock|unlock|load_file|outfile|dumpfile|"
                        r"set|begin|commit|rollback|savepoint|release|handler|do|into|pg_sleep|sleep|benchmark|"
                        r"lo_import|lo_export|dblink)\b", re.I)


class SqlError(ValueError):
    pass


def check_sql(sql: str) -> str:
    """One read-only SELECT, or refuse."""
    s = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql or "", flags=re.S).strip()
    s = s.rstrip(";").strip()
    if not s:
        raise SqlError("The query is empty.")
    if ";" in re.sub(r"'(?:[^']|'')*'|\"[^\"]*\"", "", s):
        raise SqlError("Only one statement, please (no semicolons in the middle).")
    if not re.match(r"(?is)^(select|with)\b", s):
        raise SqlError("Only SELECT queries are allowed (or WITH ... SELECT).")
    bare = re.sub(r"'(?:[^']|'')*'", "''", s)   # words inside quoted values are fine
    if re.search(r"(?i)\breplace\s+into\b", bare):
        raise SqlError("'REPLACE INTO' isn't allowed: questions can only read.")
    m = _FORBIDDEN.search(bare)
    if m:
        raise SqlError(f"'{m.group(1).upper()}' isn't allowed: questions can only read.")
    return s


@dataclass
class Question:
    phrase: str
    sql: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


@dataclass
class Connection:
    name: str
    kind: str
    path: str = ""          # sqlite / json: a file in the library folder (or an absolute path)
    host: str = ""
    port: int = 0
    user: str = ""
    password: str = ""
    database: str = ""
    questions: list[Question] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def where(self) -> str:
        if self.kind in ("sqlite", "json"):
            return self.path
        return f"{self.user + '@' if self.user else ''}{self.host}:{self.port or default_port(self.kind)}/{self.database}"


def default_port(kind: str) -> int:
    return {"mysql": 3306, "postgres": 5432}.get(kind, 0)


# =============================================================================
# Running a query
# =============================================================================
def _file(conn: Connection) -> Path:
    p = Path(conn.path)
    if not p.is_absolute():
        p = (config.LIBRARY_DIR / p).resolve()
    return p


def json_to_sqlite(path: Path) -> sqlite3.Connection:
    """A JSON file as tables: a list of objects is table `data`; {"orders": [...], ...} gives a table per key."""
    data = json.loads(path.read_text(encoding="utf-8"))
    tables = {"data": data} if isinstance(data, list) else \
        {k: v for k, v in data.items() if isinstance(v, list)} if isinstance(data, dict) else {}
    if isinstance(data, dict) and not tables:
        tables = {"data": [data]}
    db = sqlite3.connect(":memory:")
    for name, rows in tables.items():
        rows = [flat(r) if isinstance(r, dict) else {"value": r} for r in rows]
        cols: list[str] = []
        for r in rows:
            for c in r:
                if c not in cols:
                    cols.append(c)
        if not cols:
            continue
        tname = re.sub(r"\W", "_", name) or "data"
        db.execute(f'CREATE TABLE "{tname}" ({", ".join(f"{_q(c)}" for c in cols)})')
        db.executemany(f'INSERT INTO "{tname}" VALUES ({", ".join("?" for _ in cols)})',
                       [[_val(r.get(c)) for c in cols] for r in rows])
    return db


def _q(col: str) -> str:
    return '"' + col.replace('"', '""') + '"'


def _val(v):
    return json.dumps(v) if isinstance(v, (list, dict)) else v


def flat(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}_{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(flat(v, key))
        else:
            out[key] = v
    return out


def run_query(conn: Connection, sql: str, params: dict | None = None, trusted: bool = False) -> tuple[list[str], list[tuple]]:
    """(columns, rows) — at most MAX_ROWS rows. trusted=True only for our own listing queries."""
    if not trusted:
        sql = check_sql(sql)
    params = params or {}
    if conn.kind in ("sqlite", "json"):
        path = _file(conn)
        if not path.exists():
            raise SqlError(f"{conn.path} isn't there.")
        if conn.kind == "sqlite":
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=TIMEOUT_S)
        else:
            db = json_to_sqlite(path)
        try:
            deadline = time.monotonic() + TIMEOUT_S
            db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
            cur = db.execute(sql, params)
            cols = [d[0] for d in cur.description or []]
            return cols, cur.fetchmany(MAX_ROWS)
        finally:
            db.close()
    if conn.kind == "mysql":
        import pymysql
        db = pymysql.connect(host=conn.host, port=conn.port or 3306, user=conn.user, password=conn.password,
                             database=conn.database or None, connect_timeout=TIMEOUT_S, read_timeout=TIMEOUT_S,
                             charset="utf8mb4")
        try:
            with db.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
                cur.execute("START TRANSACTION READ ONLY")
                cur.execute(re.sub(r":([A-Za-z_]\w*)", r"%(\1)s", sql.replace("%", "%%")), params)
                cols = [d[0] for d in cur.description or []]
                rows = cur.fetchmany(MAX_ROWS)
                db.rollback()
                return cols, rows
        finally:
            db.close()
    if conn.kind == "postgres":
        import pg8000.native
        db = pg8000.native.Connection(user=conn.user, password=conn.password or None, host=conn.host,
                                      port=conn.port or 5432, database=conn.database or None, timeout=TIMEOUT_S)
        try:
            db.run("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            db.run(f"SET statement_timeout = {TIMEOUT_S * 1000}")
            rows = db.run(sql, **params)
            cols = [c["name"] for c in db.columns or []]
            return cols, [tuple(r) for r in (rows or [])[:MAX_ROWS]]
        finally:
            db.close()
    raise SqlError(f"Unknown database kind {conn.kind}")


def list_tables(conn: Connection) -> list[str]:
    """For the 'Test connection' button."""
    if conn.kind in ("sqlite", "json"):
        _c, rows = run_query(conn, "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name",
                             trusted=True)
    elif conn.kind == "mysql":
        _c, rows = run_query(conn, "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() "
                                   "ORDER BY table_name", trusted=True)
    else:
        _c, rows = run_query(conn, "SELECT table_schema || '.' || table_name FROM information_schema.tables "
                                   "WHERE table_schema NOT IN ('pg_catalog','information_schema') ORDER BY 1",
                             trusted=True)
    return [str(r[0]) for r in rows]


# =============================================================================
# Saved connections
# =============================================================================
class Databases:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (config.DATA_DIR / "library" / "databases.json")
        self._lock = threading.RLock()
        self.conns: list[Connection] = []
        self.last_error: dict[str, str] = {}   # question id -> what went wrong last time (shown on the page)
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except Exception as exc:
            log.error("Couldn't read %s: %s", self.path, exc)
            return
        self.conns = []
        for c in raw:
            qs = [Question(**q) for q in c.pop("questions", [])]
            self.conns.append(Connection(**c, questions=qs))

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps([asdict(c) for c in self.conns], indent=2))
            os.chmod(tmp, 0o600)   # it holds database passwords
            tmp.replace(self.path)

    def get(self, cid: str) -> Connection | None:
        return next((c for c in self.conns if c.id == cid), None)

    def add(self, conn: Connection) -> Connection:
        with self._lock:
            self.conns.append(conn)
            self.save()
        return conn

    def remove(self, cid: str) -> None:
        with self._lock:
            self.conns = [c for c in self.conns if c.id != cid]
            self.save()

    def add_question(self, cid: str, phrase: str, sql: str) -> Question:
        check_sql(sql)
        with self._lock:
            conn = self.get(cid)
            if not conn:
                raise SqlError("No such connection.")
            q = Question(phrase.strip(), sql.strip())
            conn.questions.append(q)
            self.save()
            return q

    def remove_question(self, cid: str, qid: str) -> None:
        with self._lock:
            conn = self.get(cid)
            if conn:
                conn.questions = [q for q in conn.questions if q.id != qid]
                self.save()

    # ------------------------------------------------------------ matching what was said
    def match(self, text: str) -> tuple[Connection, Question, dict] | None:
        best, best_score = None, 0.0
        said = _norm(text)
        for conn in self.conns:
            for q in conn.questions:
                params = _placeholders(q.phrase, said)
                if params is not None:
                    score = 1.0 + len(_words(q.phrase))     # a phrase that fits exactly beats everything
                elif "{" in q.phrase:
                    continue
                else:
                    want, got = _words(q.phrase), _words(said)
                    if not want:
                        continue
                    score = len(want & got) / len(want)
                    if score < 0.75 or len(want & got) < min(2, len(want)):
                        continue
                    params = {}
                if score > best_score:
                    best, best_score = (conn, q, params), score
        return best

    @staticmethod
    def context(conn: Connection, q: Question, cols: list[str], rows: list[tuple], heard: str) -> str:
        lines = [" | ".join(cols)] + [" | ".join("" if v is None else str(v) for v in r) for r in rows[:MAX_ROWS]]
        more = " (only the first 50 rows are shown)" if len(rows) >= MAX_ROWS else ""
        return privacy.mark(
            f"The user asked their own database '{conn.name}' a saved question ('{q.phrase}'). The query returned "
            f"{len(rows)} row{'s' if len(rows) != 1 else ''}{more}:\n" + "\n".join(lines) +
            "\n\nAnswer the user's question from these results in one or two spoken sentences. Read numbers "
            "naturally. If there are no rows, say there's nothing matching. Don't mention SQL or tables.")


def _norm(text: str) -> str:
    return re.sub(r"[^\w' ]+", " ", text.lower()).strip()


_FILLER = set("a an the is are was what what's whats how many much do does did i we you me my our please jarvis tell "
              "can could of for to in on at".split())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[\w']+", re.sub(r"\{\w+\}", " ", text.lower())) if w not in _FILLER}


def _placeholders(phrase: str, said: str) -> dict | None:
    """'phone number for {name}' against 'what's the phone number for Sam Jones' -> {'name': 'sam jones'}."""
    names = re.findall(r"\{(\w+)\}", phrase)
    if not names:
        return None
    pattern = ""
    for part in re.split(r"(\{\w+\})", phrase):
        m = re.fullmatch(r"\{(\w+)\}", part)
        if m:
            pattern += rf"(?P<{m.group(1)}>.+?)"
            continue
        words = _norm(part).split()
        if words:
            pattern += r"\W*" + r"\W+".join(map(re.escape, words)) + r"\W*"
    m = re.search(pattern + r"$", said)
    if not m:
        return None
    return {k: v.strip() for k, v in m.groupdict().items() if v.strip()} or None
