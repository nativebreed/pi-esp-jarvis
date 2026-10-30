"""The Library page: what's in your private library, a test search, and your database connections.

Open http://<pi>:8000/library. Nothing on this page is ever sent to Claude.
"""
from __future__ import annotations

import asyncio
import html
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path
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
  a.t { color:var(--text); text-decoration:none; } a.t:hover { color:var(--accent); }
  button.x, a.x { border:none; color:var(--muted); font-size:18px; padding:0 4px; text-decoration:none; line-height:1; }
  button.x:hover { color:var(--bad); }
  .drop { display:flex; align-items:center; justify-content:center; border:2px dashed var(--line); border-radius:12px;
          padding:22px 12px; color:var(--muted); cursor:pointer; text-align:center; margin:0 0 4px; font-size:15px; }
  .drop.on, .drop:hover { border-color:var(--accent); color:var(--text); }
  .drop input { display:none; }
  .bar { height:6px; background:#0f131a; border-radius:3px; margin-top:10px; overflow:hidden; }
  .bar i { display:block; height:100%; width:0; background:var(--accent); transition:width .2s; }
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
            if f["path"].startswith("voice notes/"):
                name = f'<span class="t">{_esc(f["path"])}</span>'
                remove = '<a class="x" href="/notes" title="Voice notes are managed on the Voice notes page">↗</a>'
            else:
                name = f'<a class="t" href="/library/file?path={quote(f["path"])}" target="_blank">{_esc(f["path"])}</a>'
                remove = (f'<form method="post" action="/library/file/delete" class="inline" '
                          f'onsubmit="return confirm(\'Delete {_esc(f["path"].replace(chr(39), "").replace(chr(92), ""))} from the Pi? '
                          f'This can\\\'t be undone.\')"><input type="hidden" name="path" value="{_esc(f["path"])}">'
                          f'<button class="x" title="Delete from the library">×</button></form>')
            items.append(f'<li><div class="file">{name}'
                         f'<span class="k">{_esc(f["kind"])} · {_size(f["size"])}</span>'
                         f'<span class="tag {cls}">{word}</span>{remove}</div>{detail}</li>')
        for p in sorted(lib.queued)[:50]:
            items.append(f'<li><div class="file"><span class="t">{_esc(p)}</span><span class="tag">waiting</span></div></li>')
        file_list = "".join(items) or ('<li class="small">No files yet. Add some above, or copy them into the '
                                       '<b>library</b> folder on the Pi.</li>')
        more = f'<p class="small">Showing 500 of {len(files)} files.</p>' if len(files) > 500 else ""
        files_html = f"""<section><h2>Files</h2><ul>{file_list}</ul>{more}
          <details><summary>What Jarvis can read</summary><p class="small">Text and Markdown, PDF (scans too),
          Word (.docx), Excel (.xlsx), PowerPoint (.pptx), OpenDocument text, RTF, CSV, JSON, web pages, pictures
          (the text in them, plus a short description), sound recordings (transcribed), and your voice notes.
          Old .doc / .xls / .ppt files: save them in the newer format. Database files are skipped here: add them
          under Databases.</p></details></section>"""

        folders = sorted({str(Path(f["path"]).parent) for f in files
                          if not f["path"].startswith("voice notes/") and str(Path(f["path"]).parent) != "."})
        folder_opts = "".join(f'<option value="{_esc(d)}">' for d in folders)
        upload = f"""<section id="add"><h2>Add files</h2>
          <form id="up" method="post" action="/library/upload" enctype="multipart/form-data">
            <!-- the folder comes first, so the Pi knows where to put the files as they arrive -->
            <label style="margin-top:0">Folder (optional)</label>
            <input type="text" name="folder" list="folders" placeholder="e.g. home/insurance" maxlength="120"
              autocapitalize="off" style="margin-bottom:10px"><datalist id="folders">{folder_opts}</datalist>
            <label class="drop" id="drop"><input type="file" name="file" id="pick" multiple>
              <span id="droptext">Drop files here, or <u>choose files</u></span></label>
            <button class="go" id="upbtn" style="margin-top:8px;width:100%">Upload</button>
            <div class="bar" id="bar" style="display:none"><i id="barfill"></i></div>
          </form>
          <p class="small" style="margin:8px 0 0">Up to {config.LIBRARY_MAX_MB:g} MB a file. Jarvis reads new files within a
          minute. A file with the same name in the same folder is kept: the new one gets "(2)" added.</p></section>
          <script>
          (function(){{var f=document.getElementById('up'),pick=document.getElementById('pick'),drop=document.getElementById('drop'),
            t=document.getElementById('droptext'),bar=document.getElementById('bar'),fill=document.getElementById('barfill');
            function show(){{var n=pick.files.length;t.textContent=n?(n==1?pick.files[0].name:n+' files chosen'):'Drop files here, or choose files';}}
            pick.addEventListener('change',show);
            ['dragenter','dragover'].forEach(function(e){{drop.addEventListener(e,function(ev){{ev.preventDefault();drop.classList.add('on');}});}});
            ['dragleave','drop'].forEach(function(e){{drop.addEventListener(e,function(ev){{ev.preventDefault();drop.classList.remove('on');}});}});
            drop.addEventListener('drop',function(ev){{pick.files=ev.dataTransfer.files;show();}});
            f.addEventListener('submit',function(ev){{if(!pick.files.length){{ev.preventDefault();t.textContent='Choose a file first';return;}}
              if(!window.XMLHttpRequest||!window.FormData)return;ev.preventDefault();
              var x=new XMLHttpRequest();x.open('POST',f.action);bar.style.display='';document.getElementById('upbtn').disabled=true;
              x.upload.onprogress=function(e){{if(e.lengthComputable)fill.style.width=(100*e.loaded/e.total)+'%';}};
              x.onload=function(){{location.href=x.responseURL||'/library';}};
              x.onerror=function(){{t.textContent='Upload failed: is the Pi still reachable?';document.getElementById('upbtn').disabled=false;}};
              x.send(new FormData(f));}});}})();
          </script>"""

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

        notice_top = ""
        if req.query.get("fmsg"):
            notice_top = f'<div class="note ok">{_esc(req.query["fmsg"])}</div>'
        elif req.query.get("ferr"):
            notice_top = f'<div class="note bad">{_esc(req.query["ferr"])}</div>'
        body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{'<meta http-equiv="refresh" content="20">' if (lib.queued or lib.busy) and not (q or test_id or try_id) else ''}
<title>Jarvis library</title><style>{STYLE}</style></head><body><main>
<h1><span>●</span> Jarvis</h1>
<nav><a href="/lists">Lists</a><a href="/notes">Voice notes</a><a class="on" href="/library">Library</a><a href="/wifi">Wi-Fi</a></nav>
<div class="lock"><b>Private.</b> Your library and databases are only ever read by the AI running on this Pi.
Nothing on this page, and no answer made from it, is sent to Claude, even if you say "use Claude".</div>
{status}{notice_top}{upload}{search}{db_html}{files_html}
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

    # ------------------------------------------------------------ adding and removing files
    def inside(rel: str) -> Path:
        """A path in the library folder, or 404 (no '..', no voice notes, no hidden files)."""
        rel = rel.strip().lstrip("/")
        if not rel or rel.startswith("voice notes/") or any(p.startswith(".") for p in rel.split("/")):
            raise web.HTTPNotFound()
        root = lib.dir.resolve()
        target = (root / rel).resolve()
        if root not in target.parents:
            raise web.HTTPNotFound()
        return target

    def own(path: Path) -> None:
        """Give new files and folders to whoever owns the library folder (not root), so they can manage them too."""
        try:
            st = lib.dir.stat()
            os.chown(path, st.st_uid, st.st_gid)
        except (OSError, AttributeError):
            pass

    def back_files(**params) -> web.HTTPSeeOther:
        query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
        return web.HTTPSeeOther("/library" + (f"?{query}" if query else "") + "#add")

    async def upload(req: web.Request):
        limit = int(config.LIBRARY_MAX_MB * 1024 * 1024)
        try:
            reader = await req.multipart()
        except Exception:
            raise back_files(ferr="That upload didn't arrive properly. Try again.")
        folder = ""
        saved, refused = [], []
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "folder":
                folder = clean_folder((await part.text())[:200])
                continue
            if part.name != "file" or not part.filename:
                await part.release()
                continue
            name = clean_name(part.filename)
            if not name:
                refused.append(f"{part.filename} (not a usable file name)")
                await part.release()
                continue
            try:
                lib.dir.mkdir(parents=True, exist_ok=True)
                dest_dir = inside(folder + "/x").parent if folder else lib.dir.resolve()
            except web.HTTPNotFound:
                raise back_files(ferr="That folder name isn't allowed.")
            if not dest_dir.exists():
                dest_dir.mkdir(parents=True, exist_ok=True)
                d = dest_dir
                while d != lib.dir.resolve():   # each new folder level belongs to the library's owner
                    own(d)
                    d = d.parent
            tmp = dest_dir / f".upload-{uuid.uuid4().hex[:8]}"
            size, too_big = 0, False
            try:
                with open(tmp, "wb") as fh:
                    while True:
                        chunk = await part.read_chunk(256 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > limit:
                            too_big = True
                            break
                        fh.write(chunk)
                if too_big:
                    tmp.unlink(missing_ok=True)
                    await part.release()
                    refused.append(f"{name} (bigger than {config.LIBRARY_MAX_MB:g} MB)")
                    continue
                dest = unique(dest_dir / name)
                os.replace(tmp, dest)
                own(dest)
                saved.append(dest.relative_to(lib.dir.resolve()).as_posix())
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
        if saved:
            await lib.scan()   # shows them as waiting straight away
        msg = ""
        if len(saved) == 1:
            msg = f"Added {saved[0]}. Jarvis will read it in a moment."
        elif saved:
            msg = f"Added {len(saved)} files. Jarvis will read them in a moment."
        if refused:
            err = "Not added: " + "; ".join(refused)
            raise back_files(**({"fmsg": msg, "ferr": err} if msg else {"ferr": err}))
        if not saved:
            raise back_files(ferr="Choose at least one file.")
        raise back_files(fmsg=msg)

    async def delete_file(req: web.Request):
        f = await req.post()
        rel = str(f.get("path", ""))
        target = inside(rel)
        if not target.is_file():
            raise back_files(ferr=f"{rel} isn't there any more.")
        target.unlink()
        lib.forget(target.relative_to(lib.dir.resolve()).as_posix())
        d = target.parent   # tidy up folders left empty
        while d != lib.dir.resolve() and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent
        raise back_files(fmsg=f"Deleted {rel}. Jarvis has forgotten it.")

    async def open_file(req: web.Request) -> web.StreamResponse:
        target = inside(req.query.get("path", ""))
        if not target.is_file():
            raise web.HTTPNotFound()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        inline = ctype.startswith(("image/", "audio/", "text/plain")) or ctype == "application/pdf"
        disp = "inline" if inline else "attachment"
        return web.FileResponse(target, headers={
            "Content-Type": "text/plain; charset=utf-8" if ctype.startswith("text/") else ctype,
            "Content-Disposition": f"{disp}; filename*=UTF-8''{quote(target.name)}",
            "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})

    app.router.add_post("/library/upload", upload)
    app.router.add_post("/library/file/delete", delete_file)
    app.router.add_get("/library/file", open_file)
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


def clean_name(name: str) -> str:
    """A safe file name: no folders, no hidden files, no odd characters."""
    from urllib.parse import unquote
    if re.search(r"%[0-9A-Fa-f]{2}", name):   # some clients percent-encode the name
        name = unquote(name)
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "", name).strip()
    if not name or name.startswith("."):
        return ""
    return name.rstrip(". ")[:150]


def clean_folder(folder: str) -> str:
    parts = [clean_name(p) for p in folder.replace("\\", "/").split("/")]
    return "/".join(p for p in parts if p and p != "..")[:120]


def unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while (path.parent / f"{stem} ({n}){suffix}").exists():
        n += 1
    return path.parent / f"{stem} ({n}){suffix}"
