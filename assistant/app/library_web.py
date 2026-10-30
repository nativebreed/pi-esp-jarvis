"""The Library page: what's in your private library, a test search, and your database connections.

Open http://<pi>:8000/library. Nothing on this page is ever sent to Claude.
"""
from __future__ import annotations

import asyncio
import html
import time
from urllib.parse import quote

from aiohttp import web

from . import config, dbs
from .dbs import Connection, Databases, SqlError
from .library import Library

STYLE = """
  :root { --bg:#0b0d12; --card:#151922; --line:#232a36; --text:#e8ecf2; --muted:#8a94a6; --accent:#2ee6ff;
          --ok:#5be38a; --warn:#ffaa28; --bad:#ff4650; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:16px/1.45 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
  main { max-width:760px; margin:0 auto; padding:20px 16px 60px; }
  h1 { font-size:22px; margin:6px 0 18px; } h1 span { color:var(--accent); }
  nav { display:flex; gap:14px; margin:-8px 0 16px; font-size:14px; flex-wrap:wrap; }
  nav a { color:var(--muted); text-decoration:none; } nav a.on { color:var(--accent); }
  section { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px; margin-bottom:14px; }
  h2 { font-size:15px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin:0 0 10px;
       display:flex; justify-content:space-between; align-items:center; gap:8px; }
  .lock { background:#10202a; border:1px solid #1d4150; border-radius:12px; padding:10px 12px; margin-bottom:14px; font-size:14px; }
  .lock b { color:var(--accent); }
  .stats { display:grid; grid-template-columns:repeat(4, 1fr); gap:8px; }
  .stat { background:#0f131a; border:1px solid var(--line); border-radius:10px; padding:8px 10px; }
  .stat .n { font-size:22px; font-weight:650; } .stat .l { font-size:12px; color:var(--muted); }
  @media (max-width:520px) { .stats { grid-template-columns:repeat(2, 1fr); } }
  input[type=text], input[type=password], input[type=number], select, textarea {
    width:100%; font:inherit; color:var(--text); background:#0f131a; border:1px solid var(--line); border-radius:9px; padding:8px 10px; }
  textarea { font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:14px; min-height:70px; }
  label { display:block; font-size:13px; color:var(--muted); margin:8px 0 3px; }
  .row { display:flex; gap:8px; } .row > * { flex:1; min-width:0; }
  button { font:inherit; font-size:14px; color:var(--text); background:transparent; border:1px solid var(--line);
           border-radius:9px; padding:5px 11px; cursor:pointer; }
  button.go { background:var(--accent); color:#000; border-color:var(--accent); font-weight:600; }
  button.del { color:var(--bad); border-color:#4a2228; }
  form { margin:0; } .inline { display:inline; }
  ul { list-style:none; padding:0; margin:0; }
  li { padding:8px 2px; border-top:1px solid var(--line); } li:first-child { border-top:none; }
  .file { display:flex; gap:10px; align-items:baseline; }
  .file .t { flex:1; word-break:break-word; } .file .k { font-size:12px; color:var(--muted); white-space:nowrap; }
  .tag { font-size:12px; border-radius:99px; padding:2px 8px; border:1px solid var(--line); color:var(--muted); white-space:nowrap; }
  .tag.ok { color:var(--ok); border-color:#25503a; } .tag.warn { color:var(--warn); border-color:#5a4220; }
  .tag.bad { color:var(--bad); border-color:#4a2228; }
  .detail { font-size:13px; color:var(--muted); margin-top:2px; }
  .hit { padding:10px 2px; border-top:1px solid var(--line); } .hit:first-child { border-top:none; }
  .hit .src { font-size:13px; color:var(--accent); } .hit .txt { font-size:14px; margin-top:3px; white-space:pre-wrap; }
  .score { font-size:12px; color:var(--muted); float:right; }
  code, pre { font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:13px; }
  pre { background:#0f131a; border:1px solid var(--line); border-radius:8px; padding:8px; overflow-x:auto; margin:6px 0; white-space:pre-wrap; }
  table.res { border-collapse:collapse; width:100%; font-size:13px; margin-top:6px; display:block; overflow-x:auto; }
  table.res th, table.res td { border:1px solid var(--line); padding:4px 6px; text-align:left; white-space:nowrap; }
  .db { border:1px solid var(--line); border-radius:12px; padding:12px; margin-bottom:12px; }
  .db .head { display:flex; justify-content:space-between; align-items:baseline; gap:8px; flex-wrap:wrap; }
  .db .name { font-weight:650; } .db .where { font-size:13px; color:var(--muted); word-break:break-all; }
  .q { border-top:1px solid var(--line); padding:8px 0; } .q .p { font-weight:600; }
  .actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:6px; }
  .note { border-radius:10px; padding:10px 12px; margin:8px 0; font-size:14px; }
  .note.ok { background:#0f2418; border:1px solid #25503a; } .note.bad { background:#2a1216; border:1px solid #4a2228; }
  details summary { cursor:pointer; color:var(--accent); font-size:14px; margin:6px 0; }
  .small { font-size:13px; color:var(--muted); }
"""

STATUS_TAG = {"indexed": ("ok", "ready"), "failed": ("bad", "couldn't read"), "skipped": ("warn", "skipped"),
              "waiting": ("", "waiting")}


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _size(n: int) -> str:
    n = n or 0
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _result_table(cols: list[str], rows: list[tuple]) -> str:
    if not cols:
        return '<p class="small">No columns came back.</p>'
    head = "".join(f"<th>{_esc(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{_esc(v)}</td>" for v in r) + "</tr>" for r in rows[:20])
    more = f'<p class="small">Showing 20 of {len(rows)} rows.</p>' if len(rows) > 20 else ""
    return (f'<table class="res"><tr>{head}</tr>{body}</table>{more}' if rows
            else '<p class="small">The query worked but found no rows.</p>')


def add_routes(app: web.Application, lib: Library, databases: Databases) -> None:

    async def page(req: web.Request) -> web.Response:
        q = req.query.get("q", "").strip()[:200]
        err, msg = req.query.get("err", ""), req.query.get("msg", "")
        test_id, try_id = req.query.get("test", ""), req.query.get("try", "")
        st = lib.stats()
        if lib.embed_ready:
            model = f'<span class="tag ok">search model ready</span>'
        elif lib.embed_error:
            model = f'<span class="tag bad">search model problem</span> <span class="small">{_esc(lib.embed_error[:200])}</span>'
        else:
            model = '<span class="tag warn">search model loading…</span>'
        busy = f'<p class="small">Reading now: {_esc(lib.busy)}</p>' if lib.busy else ""
        scanned = time.strftime("%H:%M:%S", time.localtime(lib.last_scan)) if lib.last_scan else "not yet"

        status = f"""<section><h2>Your library
          <form method="post" action="/library/scan"><button>Look for new files now</button></form></h2>
          <div class="stats">
            <div class="stat"><div class="n">{st['indexed']}</div><div class="l">files ready</div></div>
            <div class="stat"><div class="n">{st['passages']}</div><div class="l">passages</div></div>
            <div class="stat"><div class="n">{st['waiting']}</div><div class="l">waiting to be read</div></div>
            <div class="stat"><div class="n">{st['failed'] + st['skipped']}</div><div class="l">skipped or unreadable</div></div>
          </div>
          <p class="small" style="margin:10px 0 0">{model} &nbsp; Folder on the Pi: <b>library</b> (next to your music folder).
          Last looked for changes at {scanned}; new and changed files are picked up within {int(config.LIBRARY_SCAN_S)} seconds.
          {"Your voice notes are included." if config.LIBRARY_VOICE_NOTES else ""}</p>{busy}</section>"""

        results = ""
        if q:
            hits = await lib.search(q, k=5) if not lib.empty() else []
            if hits:
                rows = "".join(
                    f'<div class="hit"><span class="score">match {h.score:.2f}'
                    f'{" · would answer by itself" if h.score >= config.LIBRARY_MIN_SCORE else ""}</span>'
                    f'<div class="src">{_esc(h.path)}{(" · " + _esc(h.loc)) if h.loc else ""}</div>'
                    f'<div class="txt">{_esc(h.text[:500])}{"…" if len(h.text) > 500 else ""}</div></div>'
                    for h in hits)
                results = f'<div style="margin-top:10px">{rows}</div>'
            else:
                results = '<p class="small">Nothing found.</p>'
        search = f"""<section><h2>Try a search</h2>
          <form method="get" action="/library" class="row"><input type="text" name="q" value="{_esc(q)}"
            placeholder="e.g. when does the car insurance renew"><button class="go" style="flex:0 0 auto">Search</button></form>
          <p class="small" style="margin:6px 0 0">These are the passages Jarvis would read before answering. A match of
          {config.LIBRARY_MIN_SCORE:.2f} or more is used automatically; say "check my files for…" to search harder.</p>{results}</section>"""

        files = lib.files()
        order = {"failed": 0, "skipped": 1, "indexed": 2}
        files.sort(key=lambda f: (order.get(f["status"], 3), f["path"].lower()))
        items = []
        for f in files[:500]:
            cls, word = STATUS_TAG.get(f["status"], ("", f["status"]))
            if f["status"] == "indexed":
                word = f'{f["chunks"]} passage{"s" if f["chunks"] != 1 else ""}'
            detail = f'<div class="detail">{_esc(f["detail"])}</div>' if f["detail"] else ""
            items.append(f'<li><div class="file"><span class="t">{_esc(f["path"])}</span>'
                         f'<span class="k">{_esc(f["kind"])} · {_size(f["size"])}</span>'
                         f'<span class="tag {cls}">{word}</span></div>{detail}</li>')
        for p in sorted(lib.queued)[:50]:
            items.append(f'<li><div class="file"><span class="t">{_esc(p)}</span><span class="tag">waiting</span></div></li>')
        file_list = "".join(items) or ('<li class="small">No files yet. Copy documents, pictures or recordings into '
                                       'the <b>library</b> folder on the Pi.</li>')
        more = f'<p class="small">Showing 500 of {len(files)} files.</p>' if len(files) > 500 else ""
        files_html = f"""<section><h2>Files</h2><ul>{file_list}</ul>{more}
          <details><summary>What Jarvis can read</summary><p class="small">Text and Markdown, PDF (scans too),
          Word (.docx), Excel (.xlsx), PowerPoint (.pptx), OpenDocument text, RTF, CSV, JSON, web pages, pictures
          (the text in them, plus a short description), sound recordings (transcribed), and your voice notes.
          Old .doc / .xls / .ppt files: save them in the newer format. Database files are skipped here: add them
          under Databases.</p></details></section>"""

        conns = []
        for c in databases.conns:
            extra = ""
            if test_id == c.id:
                try:
                    tables = await asyncio.to_thread(dbs.list_tables, c)
                    extra = (f'<div class="note ok">Connected. Tables: {_esc(", ".join(tables[:60])) or "none"}'
                             f'{"…" if len(tables) > 60 else ""}</div>')
                except Exception as exc:
                    extra = f'<div class="note bad">Couldn\'t connect: {_esc(exc)}</div>'
            qs = []
            for qn in c.questions:
                res = ""
                if try_id == qn.id:
                    try:
                        cols, rows = await asyncio.to_thread(dbs.run_query, c, qn.sql, _sample_params(qn.phrase))
                        res = _result_table(cols, rows)
                    except Exception as exc:
                        res = f'<div class="note bad">{_esc(exc)}</div>'
                elif databases.last_error.get(qn.id):
                    res = f'<div class="note bad">Last time: {_esc(databases.last_error[qn.id])}</div>'
                qs.append(f"""<div class="q"><div class="p">"{_esc(qn.phrase)}"</div><pre>{_esc(qn.sql)}</pre>
                  <div class="actions"><form method="post" action="/library/db/try"><input type="hidden" name="cid" value="{c.id}">
                  <input type="hidden" name="qid" value="{qn.id}"><button>Try it</button></form>
                  <form method="post" action="/library/db/qdel" onsubmit="return confirm('Delete this question?')">
                  <input type="hidden" name="cid" value="{c.id}"><input type="hidden" name="qid" value="{qn.id}">
                  <button class="del">Delete</button></form></div>{res}</div>""")
            conns.append(f"""<div class="db" id="db-{c.id}"><div class="head"><span class="name">{_esc(c.name)}
              <span class="tag">{_esc(c.kind)}</span></span><span class="where">{_esc(c.where())}</span></div>
              <div class="actions"><form method="post" action="/library/db/test"><input type="hidden" name="cid" value="{c.id}">
              <button>Test connection</button></form>
              <form method="post" action="/library/db/del" onsubmit="return confirm('Remove this connection and its questions?')">
              <input type="hidden" name="cid" value="{c.id}"><button class="del">Remove</button></form></div>{extra}
              {"".join(qs) or '<p class="small">No questions yet.</p>'}
              <details><summary>Add a question</summary>
              <form method="post" action="/library/db/qadd"><input type="hidden" name="cid" value="{c.id}">
              <label>What you'll say</label><input type="text" name="phrase" required maxlength="120"
                placeholder="how many orders today   /   phone number for {{name}}">
              <label>The query (read-only SELECT)</label><textarea name="sql" required
                placeholder="SELECT COUNT(*) AS orders FROM orders WHERE date(created) = date('now')"></textarea>
              <p class="small">A <code>{{word}}</code> in the phrase becomes <code>:word</code> in the query, e.g.
              <code>WHERE name LIKE '%' || :name || '%'</code> (SQLite/Postgres) or <code>WHERE name LIKE CONCAT('%', :name, '%')</code> (MySQL).
              JSON files are tables: a list of objects is <code>data</code>; {{"orders": [...]}} is <code>orders</code>.</p>
              <button class="go" style="margin-top:8px">Save question</button></form></details></div>""")
        notice = ""
        if err:
            notice = f'<div class="note bad">{_esc(err)}</div>'
        elif msg:
            notice = f'<div class="note ok">{_esc(msg)}</div>'
        db_html = f"""<section id="databases"><h2>Databases</h2>{notice}
          {"".join(conns) or '<p class="small">No connections yet.</p>'}
          <details {"open" if not databases.conns else ""}><summary>Add a connection</summary>
          <form method="post" action="/library/db/add" autocomplete="off">
            <div class="row"><div><label>Name (what Jarvis calls it)</label><input type="text" name="name" required maxlength="60" placeholder="the shop"></div>
            <div><label>Kind</label><select name="kind" id="kind" onchange="showFields()">
              <option value="sqlite">SQLite file</option><option value="json">JSON file</option>
              <option value="mysql">MySQL / MariaDB</option><option value="postgres">PostgreSQL</option></select></div></div>
            <div id="filefields"><label>File, inside the library folder (or a full path)</label>
              <input type="text" name="path" placeholder="shop/orders.sqlite"></div>
            <div id="serverfields" style="display:none">
              <div class="row"><div><label>Server</label><input type="text" name="host" placeholder="192.168.1.20"></div>
              <div style="flex:0 0 110px"><label>Port</label><input type="number" name="port" placeholder="default"></div></div>
              <div class="row"><div><label>Database</label><input type="text" name="database"></div>
              <div><label>User (read-only is best)</label><input type="text" name="user" autocapitalize="off"></div></div>
              <label>Password</label><input type="password" name="password" autocomplete="new-password">
              <p class="small">Saved on the Pi only (never shown again, never sent to Claude). Queries run read-only, but a
              database user that can only read is the safest choice.</p></div>
            <button class="go" style="margin-top:10px">Add connection</button>
          </form></details></section>
          <script>function showFields(){{var k=document.getElementById('kind').value;var f=(k=='sqlite'||k=='json');
          document.getElementById('filefields').style.display=f?'':'none';document.getElementById('serverfields').style.display=f?'none':'';}}</script>"""

        body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{'<meta http-equiv="refresh" content="20">' if (lib.queued or lib.busy) and not (q or test_id or try_id) else ''}
<title>Jarvis library</title><style>{STYLE}</style></head><body><main>
<h1><span>●</span> Jarvis</h1>
<nav><a href="/lists">Lists</a><a href="/notes">Voice notes</a><a class="on" href="/library">Library</a><a href="/wifi">Wi-Fi</a></nav>
<div class="lock"><b>Private.</b> Your library and databases are only ever read by the AI running on this Pi.
Nothing on this page, and no answer made from it, is sent to Claude, even if you say "use Claude".</div>
{status}{search}{db_html}{files_html}
<p class="small">Ask "Jarvis, check my files for the boiler warranty", "what do my documents say about the lease",
or just ask: if your files clearly have the answer, Jarvis uses them.</p>
</main></body></html>"""
        return web.Response(text=body, content_type="text/html")

    def back(**params) -> web.HTTPSeeOther:
        query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items() if k != "anchor")
        anchor = params.get("anchor", "")
        return web.HTTPSeeOther("/library" + (f"?{query}" if query else "") + (f"#{anchor}" if anchor else ""))

    async def scan(_req: web.Request):
        lib.scan_now.set()
        await asyncio.sleep(0.5)
        raise back()

    async def db_add(req: web.Request):
        f = await req.post()
        kind = str(f.get("kind", ""))
        name = str(f.get("name", "")).strip()[:60]
        if kind not in dbs.KINDS or not name:
            raise back(err="Give the connection a name and pick a kind.", anchor="databases")
        try:
            port = int(str(f.get("port", "") or 0))
        except ValueError:
            port = 0
        conn = Connection(name=name, kind=kind, path=str(f.get("path", "")).strip(), host=str(f.get("host", "")).strip(),
                          port=port, user=str(f.get("user", "")).strip(), password=str(f.get("password", "")),
                          database=str(f.get("database", "")).strip())
        if kind in ("sqlite", "json") and not conn.path:
            raise back(err="Which file? Put it in the library folder and type its name.", anchor="databases")
        if kind in ("mysql", "postgres") and not conn.host:
            raise back(err="Which server? Type its address.", anchor="databases")
        databases.add(conn)
        raise back(test=conn.id, anchor=f"db-{conn.id}")

    async def db_del(req: web.Request):
        f = await req.post()
        databases.remove(str(f.get("cid", "")))
        raise back(msg="Connection removed.", anchor="databases")

    async def db_test(req: web.Request):
        f = await req.post()
        cid = str(f.get("cid", ""))
        raise back(test=cid, anchor=f"db-{cid}")

    async def q_add(req: web.Request):
        f = await req.post()
        cid = str(f.get("cid", ""))
        phrase, sql = str(f.get("phrase", "")).strip()[:120], str(f.get("sql", "")).strip()[:4000]
        if not phrase or not sql:
            raise back(err="A question needs both a phrase and a query.", anchor=f"db-{cid}")
        try:
            q = databases.add_question(cid, phrase, sql)
        except SqlError as exc:
            raise back(err=str(exc), anchor=f"db-{cid}")
        raise back(**{"try": q.id, "anchor": f"db-{cid}"})

    async def q_del(req: web.Request):
        f = await req.post()
        cid = str(f.get("cid", ""))
        databases.remove_question(cid, str(f.get("qid", "")))
        raise back(anchor=f"db-{cid}")

    async def q_try(req: web.Request):
        f = await req.post()
        cid = str(f.get("cid", ""))
        raise back(**{"try": str(f.get("qid", "")), "anchor": f"db-{cid}"})

    app.router.add_get("/library", page)
    app.router.add_post("/library/scan", scan)
    app.router.add_post("/library/db/add", db_add)
    app.router.add_post("/library/db/del", db_del)
    app.router.add_post("/library/db/test", db_test)
    app.router.add_post("/library/db/qadd", q_add)
    app.router.add_post("/library/db/qdel", q_del)
    app.router.add_post("/library/db/try", q_try)


def _sample_params(phrase: str) -> dict:
    """'Try it' on a phrase with {placeholders}: an empty value matches everything with LIKE '%' || :x || '%'."""
    import re
    return {name: "" for name in re.findall(r"\{(\w+)\}", phrase)}
