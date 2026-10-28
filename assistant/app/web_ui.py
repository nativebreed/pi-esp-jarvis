"""A small phone-friendly page for your lists and the things Jarvis remembers.

Open http://<pi>:8000/lists on any device on your home network.
http://<pi>:8000/wifi adds Wi-Fi networks for the board, typed on a proper keyboard.
http://<pi>:8000/notes plays, reads, downloads and deletes voice notes.
"""
from __future__ import annotations

import asyncio
import html
import io
import time
import wave
from urllib.parse import quote

from typing import Callable, Optional

from aiohttp import web

from .notes import ID_RE, Note, NoteStore, Transcriber, decode_pcm16
from .store import Store

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis lists</title>
<style>
  :root {{ --bg:#0b0d12; --card:#151922; --line:#232a36; --text:#e8ecf2; --muted:#8a94a6; --accent:#2ee6ff; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text); font:16px/1.45 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }}
  main {{ max-width:640px; margin:0 auto; padding:20px 16px 60px; }}
  h1 {{ font-size:22px; margin:6px 0 18px; letter-spacing:.2px; }}
  h1 span {{ color:var(--accent); }}
  section {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px 14px 10px; margin-bottom:16px; }}
  h2 {{ font-size:15px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin:0 0 8px; display:flex; justify-content:space-between; align-items:center; }}
  ul {{ list-style:none; padding:0; margin:0; }}
  li {{ display:flex; align-items:center; gap:10px; padding:9px 2px; border-top:1px solid var(--line); }}
  li:first-child {{ border-top:none; }}
  li .t {{ flex:1; word-break:break-word; }}
  li.done .t {{ color:var(--muted); text-decoration:line-through; }}
  form {{ margin:0; display:inline; }}
  button {{ font:inherit; color:var(--text); background:transparent; border:1px solid var(--line); border-radius:9px; padding:4px 10px; cursor:pointer; }}
  button.check {{ width:28px; height:28px; padding:0; border-radius:50%; }}
  li.done button.check {{ background:var(--accent); border-color:var(--accent); color:#000; }}
  button.x {{ border:none; color:var(--muted); font-size:18px; padding:2px 6px; }}
  .add {{ display:flex; gap:8px; margin-top:10px; }}
  .add input {{ flex:1; min-width:0; font:inherit; color:var(--text); background:#0f131a; border:1px solid var(--line); border-radius:9px; padding:8px 10px; }}
  .add button {{ background:var(--accent); color:#000; border-color:var(--accent); font-weight:600; }}
  .empty {{ color:var(--muted); padding:6px 2px; }}
  .small {{ font-size:13px; color:var(--muted); }}
  nav {{ display:flex; gap:14px; margin:-8px 0 16px; font-size:14px; }}
  nav a {{ color:var(--muted); text-decoration:none; }} nav a.on {{ color:var(--accent); }}
</style></head>
<body><main>
<h1><span>●</span> Jarvis</h1>
<nav><a class="on" href="/lists">Lists</a><a href="/notes">Voice notes</a><a href="/wifi">Wi-Fi</a></nav>
{sections}
<section>
  <h2>New list</h2>
  <form class="add" method="post" action="/lists/add"><input name="list" placeholder="List name, e.g. holiday packing" required>
  <input type="hidden" name="text" value=""><button>Create</button></form>
</section>
<p class="small">Say "add milk to the shopping list", "remember the spare key is in the shed", or "what's on my to-do list".</p>
</main></body></html>"""


WIFI_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis Wi-Fi</title>
<style>
  :root {{ --bg:#0b0d12; --card:#151922; --line:#232a36; --text:#e8ecf2; --muted:#8a94a6; --accent:#2ee6ff; --ok:#5be38a; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text); font:16px/1.45 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }}
  main {{ max-width:640px; margin:0 auto; padding:20px 16px 60px; }}
  h1 {{ font-size:22px; margin:6px 0 18px; }} h1 span {{ color:var(--accent); }}
  nav {{ display:flex; gap:14px; margin:-8px 0 16px; font-size:14px; }}
  nav a {{ color:var(--muted); text-decoration:none; }} nav a.on {{ color:var(--accent); }}
  section {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px; margin-bottom:16px; }}
  h2 {{ font-size:15px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin:0 0 10px; }}
  label {{ display:block; font-size:14px; color:var(--muted); margin:10px 0 4px; }}
  input[type=text], input[type=password] {{ width:100%; font:inherit; color:var(--text); background:#0f131a;
    border:1px solid var(--line); border-radius:9px; padding:10px; }}
  .row {{ display:flex; align-items:center; gap:8px; margin:10px 0 2px; font-size:14px; color:var(--muted); }}
  button {{ font:inherit; color:var(--text); background:transparent; border:1px solid var(--line); border-radius:9px; padding:4px 10px; cursor:pointer; }}
  button.go {{ margin-top:14px; width:100%; padding:10px; background:var(--accent); border-color:var(--accent); color:#000; font-weight:600; }}
  ul {{ list-style:none; padding:0; margin:0; }}
  li {{ display:flex; align-items:center; gap:10px; padding:9px 2px; border-top:1px solid var(--line); }}
  li:first-child {{ border-top:none; }} li .t {{ flex:1; word-break:break-word; }}
  .tag {{ font-size:12px; border-radius:99px; padding:2px 8px; border:1px solid var(--line); color:var(--muted); }}
  .tag.ok {{ color:var(--ok); border-color:var(--ok); }}
  .note {{ background:#10202a; border:1px solid #1d4150; border-radius:10px; padding:10px 12px; margin-bottom:16px; }}
  .small {{ font-size:13px; color:var(--muted); }} form {{ margin:0; }}
</style></head>
<body><main>
<h1><span>●</span> Jarvis</h1>
<nav><a href="/lists">Lists</a><a href="/notes">Voice notes</a><a class="on" href="/wifi">Wi-Fi</a></nav>
{notice}
<section>
  <h2>Add a Wi-Fi network</h2>
  <form method="post" action="/wifi/add" autocomplete="off">
    <label for="ssid">Network name</label>
    <input type="text" id="ssid" name="ssid" maxlength="32" required autocapitalize="off" autocorrect="off" spellcheck="false">
    <label for="pw">Password</label>
    <input type="password" id="pw" name="password" maxlength="64" autocapitalize="off" autocorrect="off" spellcheck="false">
    <div class="row"><input type="checkbox" id="show" onclick="document.getElementById('pw').type=this.checked?'text':'password'">
      <label for="show" style="margin:0">Show password</label></div>
    <button class="go">Save for Jarvis</button>
  </form>
  <p class="small">Must be a 2.4 GHz network (the board can't use 5 GHz). Jarvis keeps its current network too,
  and connects to whichever one is in range.</p>
</section>
<section>
  <h2>Networks</h2>
  <ul>{rows}</ul>
</section>
<p class="small">Jarvis collects new networks the next time it talks to the Pi: say "Jarvis" or double-tap the screen.
The password is kept on the Pi only until Jarvis has it. Moving somewhere new? Add the network here <em>before</em> you go.</p>
</main></body></html>"""


NOTES_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{refresh}
<title>Jarvis voice notes</title>
<style>
  :root {{ --bg:#0b0d12; --card:#151922; --line:#232a36; --text:#e8ecf2; --muted:#8a94a6; --accent:#2ee6ff; --rec:#ff4650; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text); font:16px/1.45 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }}
  main {{ max-width:640px; margin:0 auto; padding:20px 16px 60px; }}
  h1 {{ font-size:22px; margin:6px 0 18px; }} h1 span {{ color:var(--accent); }}
  nav {{ display:flex; gap:14px; margin:-8px 0 16px; font-size:14px; }}
  nav a {{ color:var(--muted); text-decoration:none; }} nav a.on {{ color:var(--accent); }}
  section {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px; margin-bottom:14px; }}
  .search {{ display:flex; gap:8px; margin-bottom:16px; }}
  .search input {{ flex:1; min-width:0; font:inherit; color:var(--text); background:#0f131a; border:1px solid var(--line);
    border-radius:9px; padding:8px 10px; }}
  button, a.btn {{ font:inherit; font-size:14px; color:var(--text); background:transparent; border:1px solid var(--line);
    border-radius:9px; padding:5px 11px; cursor:pointer; text-decoration:none; display:inline-block; }}
  .search button {{ background:var(--accent); color:#000; border-color:var(--accent); font-weight:600; }}
  .head {{ display:flex; justify-content:space-between; align-items:baseline; gap:10px; }}
  .title {{ font-weight:600; font-size:17px; word-break:break-word; }}
  .when {{ font-size:13px; color:var(--muted); white-space:nowrap; }}
  .wave {{ display:flex; align-items:center; gap:3px; height:30px; margin:10px 0 4px; }}
  .wave i {{ flex:1; background:var(--accent); opacity:.55; border-radius:2px; min-height:3px; }}
  audio {{ width:100%; margin:6px 0 4px; height:36px; color-scheme:dark; }}
  .text {{ white-space:pre-wrap; margin:8px 0 4px; }}
  details {{ margin:6px 0; font-size:14px; color:var(--muted); }} details .text {{ color:var(--text); }}
  .tag {{ font-size:12px; border-radius:99px; padding:2px 8px; border:1px solid var(--line); color:var(--muted); }}
  .tag.wait {{ color:var(--accent); border-color:#1d4150; }}
  .actions {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }}
  .actions form {{ margin:0; }} .actions .del {{ color:var(--rec); border-color:#4a2228; margin-left:auto; }}
  .small {{ font-size:13px; color:var(--muted); }} .empty {{ color:var(--muted); }}
  mark {{ background:#2a4b55; color:var(--text); border-radius:3px; }}
</style></head>
<body><main>
<h1><span>●</span> Jarvis</h1>
<nav><a href="/lists">Lists</a><a class="on" href="/notes">Voice notes</a><a href="/wifi">Wi-Fi</a></nav>
<form class="search" method="get" action="/notes"><input name="q" value="{q}" placeholder="Search what you said…">
<button>Search</button></form>
{summary}
{cards}
<p class="small">Say "Jarvis, go into voice notes" or "take a note". Notes are kept on the Pi in the
<b>voice-notes</b> folder, named by date and time (YYYYMMDDHHMMSS), with a .txt transcript beside each one.</p>
</main></body></html>"""


def _fmt_len(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _note_card(note: Note, transcript: str, busy: bool, q: str) -> str:
    t = note.when
    when = time.strftime("%a %d %b %Y, %H:%M", t)
    title = html.escape(note.title or ("Writing it down…" if note.status == "pending" else "Untitled note"))
    bars = "".join(f'<i style="height:{12 + int(c) * 10}%"></i>' for c in (note.wave or "0" * 30) if c.isdigit())
    tag = ""
    if note.status == "pending":
        tag = '<span class="tag wait">' + ("transcribing now" if busy else "waiting to be transcribed") + "</span>"
    elif note.status == "empty":
        tag = '<span class="tag">no words heard</span>'
    elif note.status == "failed":
        tag = '<span class="tag">transcript failed</span>'

    def mark(text: str) -> str:
        out = html.escape(text)
        for w in {w for w in q.split() if len(w) > 1}:
            low, wl = out.lower(), html.escape(w).lower()
            i, res = 0, ""
            while True:
                j = low.find(wl, i)
                if j < 0:
                    res += out[i:]
                    break
                res += out[i:j] + "<mark>" + out[j:j + len(wl)] + "</mark>"
                i = j + len(wl)
            out = res
        return out

    body = ""
    if note.tidy:
        body = f'<div class="text">{mark(note.tidy)}</div>'
        if transcript:
            body += f'<details><summary>What Whisper heard</summary><div class="text">{mark(transcript)}</div></details>'
    elif transcript:
        body = f'<div class="text">{mark(transcript)}</div>'
    nid = note.id
    return (f'<section id="n{nid}"><div class="head"><span class="title">{title}</span>'
            f'<span class="when">{when} · {_fmt_len(note.seconds)}</span></div>'
            f'<div class="wave">{bars}</div>'
            f'<audio controls preload="none"><source src="/notes/audio/{nid}.ogg" type="audio/ogg; codecs=opus">'
            f'<source src="/notes/audio/{nid}.wav" type="audio/wav"></audio>'
            f'{tag}{body}'
            f'<div class="actions"><a class="btn" href="/notes/audio/{nid}.ogg?download=1">Download audio</a>'
            + (f'<a class="btn" href="/notes/text/{nid}.txt">Transcript</a>' if transcript else "")
            + (f'<form method="post" action="/notes/redo"><input type="hidden" name="id" value="{nid}">'
               f'<button title="Run the transcript again">Re-transcribe</button></form>' if note.status != "pending" else "")
            + f'<form method="post" action="/notes/delete" onsubmit="return confirm(\'Delete this voice note? This can\\\'t be undone.\')">'
            f'<input type="hidden" name="id" value="{nid}"><button class="del">Delete</button></form></div></section>')


def _wifi_rows(store: Store) -> str:
    rows = []
    for w in store.items("wifi_pending"):
        rows.append(f'<li><span class="t">{html.escape(w["ssid"])}</span><span class="tag">waiting for Jarvis</span>'
                    f'<form method="post" action="/wifi/remove"><input type="hidden" name="id" value="{w["id"]}">'
                    f'<button title="Remove">×</button></form></li>')
    for w in store.items("wifi_saved"):
        rows.append(f'<li><span class="t">{html.escape(w["ssid"])}</span><span class="tag ok">saved on Jarvis</span></li>')
    return "".join(rows) or '<li class="small">No networks yet.</li>'


def _list_section(name: str, items: list[dict]) -> str:
    n = html.escape(name)
    rows = "".join(
        f'<li class="{"done" if i.get("done") else ""}">'
        f'<form method="post" action="/lists/toggle"><input type="hidden" name="list" value="{n}">'
        f'<input type="hidden" name="id" value="{i["id"]}"><button class="check" title="Done">✓</button></form>'
        f'<span class="t">{html.escape(i["text"])}</span>'
        f'<form method="post" action="/lists/remove"><input type="hidden" name="list" value="{n}">'
        f'<input type="hidden" name="id" value="{i["id"]}"><button class="x" title="Remove">×</button></form></li>'
        for i in items) or '<li class="empty">Nothing here yet.</li>'
    clear = (f'<form method="post" action="/lists/clear"><input type="hidden" name="list" value="{n}">'
             f'<button>Clear</button></form>') if items else ""
    if not items and name not in ("shopping", "to-do"):
        clear = (f'<form method="post" action="/lists/delete"><input type="hidden" name="list" value="{n}">'
                 f'<button>Delete list</button></form>')
    return (f'<section><h2>{n.capitalize()} list {clear}</h2><ul>{rows}</ul>'
            f'<form class="add" method="post" action="/lists/add"><input type="hidden" name="list" value="{n}">'
            f'<input name="text" placeholder="Add to {n}…" required><button>Add</button></form></section>')


def _memory_section(mems: list[dict]) -> str:
    rows = "".join(
        f'<li><span class="t">{html.escape(m["text"])}</span>'
        f'<form method="post" action="/memory/remove"><input type="hidden" name="id" value="{m["id"]}">'
        f'<button class="x" title="Forget">×</button></form></li>' for m in mems) \
        or '<li class="empty">Nothing yet. Say "remember that…"</li>'
    return (f'<section><h2>Things Jarvis remembers</h2><ul>{rows}</ul>'
            f'<form class="add" method="post" action="/memory/add"><input name="text" placeholder="Something to remember…" required>'
            f'<button>Add</button></form></section>')


def add_routes(app: web.Application, store: Store, on_wifi: Optional[Callable[[], None]] = None,
               notes: Optional[NoteStore] = None, transcriber: Optional[Transcriber] = None) -> None:
    async def page(_req: web.Request) -> web.Response:
        names = store.list_names()
        ordered = [n for n in ("shopping", "to-do") if n in names] + sorted(n for n in names if n not in ("shopping", "to-do"))
        sections = "".join(_list_section(n, store.list_items(n)) for n in ordered)
        sections += _memory_section(store.items("memories"))
        return web.Response(text=PAGE.format(sections=sections), content_type="text/html")

    def back() -> web.Response:
        raise web.HTTPSeeOther("/lists")

    async def add(req: web.Request):
        f = await req.post()
        name = str(f.get("list", "")).strip().lower()[:40]
        text = str(f.get("text", "")).strip()[:200]
        if name:
            if text:
                store.list_add(name, [text])
            else:
                store.data["lists"].setdefault(name, [])
                store.save()
        back()

    async def toggle(req: web.Request):
        f = await req.post()
        store.list_toggle(str(f.get("list", "")), str(f.get("id", "")))
        back()

    async def remove(req: web.Request):
        f = await req.post()
        store.list_remove(str(f.get("list", "")), ids={str(f.get("id", ""))})
        back()

    async def clear(req: web.Request):
        f = await req.post()
        store.list_clear(str(f.get("list", "")))
        back()

    async def delete(req: web.Request):
        f = await req.post()
        store.list_delete(str(f.get("list", "")))
        back()

    async def mem_add(req: web.Request):
        f = await req.post()
        text = str(f.get("text", "")).strip()[:300]
        if text:
            store.add("memories", {"text": text, "created": time.time()})
        back()

    async def mem_remove(req: web.Request):
        f = await req.post()
        store.remove("memories", {str(f.get("id", ""))})
        back()

    async def wifi_page(req: web.Request) -> web.Response:
        added = req.query.get("added", "")
        notice = (f'<div class="note">Saved <b>{html.escape(added)}</b>. Say "Jarvis" or double-tap the screen '
                  f'and it will be sent to the board.</div>') if added else ""
        if any(w["ssid"] == added for w in store.items("wifi_saved")):
            notice = f'<div class="note">Jarvis has saved <b>{html.escape(added)}</b>. All done.</div>'
        return web.Response(text=WIFI_PAGE.format(notice=notice, rows=_wifi_rows(store)), content_type="text/html")

    async def wifi_add(req: web.Request):
        f = await req.post()
        ssid = str(f.get("ssid", "")).strip()[:32]
        password = str(f.get("password", ""))[:64]
        if not ssid:
            raise web.HTTPSeeOther("/wifi")
        old = {w["id"] for w in store.items("wifi_pending") if w["ssid"] == ssid}
        if old:
            store.remove("wifi_pending", old)
        store.add("wifi_pending", {"ssid": ssid, "password": password, "created": time.time()})
        if on_wifi:
            on_wifi()   # if Jarvis is connected right now, send it straight away
        raise web.HTTPSeeOther("/wifi?added=" + quote(ssid))

    async def wifi_remove(req: web.Request):
        f = await req.post()
        store.remove("wifi_pending", {str(f.get("id", ""))})
        raise web.HTTPSeeOther("/wifi")

    # ------------------------------------------------------------ voice notes
    def note_id(req: web.Request) -> str:
        nid = req.match_info.get("id", "")
        if not notes or not ID_RE.match(nid) or not notes.path(nid, "ogg").exists():
            raise web.HTTPNotFound()
        return nid

    async def notes_page(req: web.Request) -> web.Response:
        q = req.query.get("q", "").strip()[:100]
        found = notes.search(q) if (notes and q) else (list(reversed(notes.all())) if notes else [])
        busy = transcriber.busy if transcriber else None
        cards = "".join(_note_card(n, notes.transcript(n.id), busy == n.id, q) for n in found)
        total = len(notes.ids()) if notes else 0
        if q:
            summary = (f'<p class="small">{len(found)} of {total} notes match “{html.escape(q)}”. '
                       f'<a href="/notes" style="color:var(--accent)">Show all</a></p>')
        else:
            summary = (f'<p class="small">{total} note{"s" if total != 1 else ""}, newest first.</p>' if total else "")
        if not cards:
            cards = ('<section class="empty">No notes match that.</section>' if q else
                     '<section class="empty">No voice notes yet. Say “Jarvis, take a note”.</section>')
        refresh = '\n<meta http-equiv="refresh" content="15">' if any(n.status == "pending" for n in found) else ""
        return web.Response(text=NOTES_PAGE.format(refresh=refresh, q=html.escape(q), summary=summary, cards=cards),
                            content_type="text/html")

    async def note_audio(req: web.Request) -> web.StreamResponse:
        nid = note_id(req)
        headers = {"Cache-Control": "no-cache"}
        if req.query.get("download"):
            headers["Content-Disposition"] = f'attachment; filename="{nid}.ogg"'
        return web.FileResponse(notes.path(nid, "ogg"), headers={**headers, "Content-Type": "audio/ogg"})

    async def note_wav(req: web.Request) -> web.Response:
        """The same note as WAV, for browsers that can't play Opus in Ogg (older iPhones)."""
        nid = note_id(req)
        pcm = await asyncio.to_thread(decode_pcm16, notes.path(nid, "ogg"))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        return web.Response(body=buf.getvalue(), content_type="audio/wav")

    async def note_text(req: web.Request) -> web.Response:
        nid = note_id(req)
        note = notes.get(nid)
        text = notes.transcript(nid)
        if note and note.tidy:
            text = f"{note.title}\n\n{note.tidy}\n\n--- transcript ---\n{text}"
        return web.Response(text=text + "\n", content_type="text/plain", charset="utf-8",
                            headers={"Content-Disposition": f'inline; filename="{nid}.txt"'})

    async def note_delete(req: web.Request):
        f = await req.post()
        nid = str(f.get("id", ""))
        if notes and ID_RE.match(nid):
            notes.delete(nid)
        raise web.HTTPSeeOther("/notes")

    async def note_redo(req: web.Request):
        f = await req.post()
        nid = str(f.get("id", ""))
        note = notes.get(nid) if (notes and ID_RE.match(nid)) else None
        if note and transcriber:
            note.status = "pending"
            notes.save_meta(note)
            transcriber.add(nid)
        raise web.HTTPSeeOther(f"/notes#n{nid}")

    app.router.add_get("/notes", notes_page)
    app.router.add_get("/notes/audio/{id}.ogg", note_audio)
    app.router.add_get("/notes/audio/{id}.wav", note_wav)
    app.router.add_get("/notes/text/{id}.txt", note_text)
    app.router.add_post("/notes/delete", note_delete)
    app.router.add_post("/notes/redo", note_redo)
    app.router.add_get("/wifi", wifi_page)
    app.router.add_post("/wifi/add", wifi_add)
    app.router.add_post("/wifi/remove", wifi_remove)
    app.router.add_get("/lists", page)
    app.router.add_get("/", page)
    app.router.add_post("/lists/add", add)
    app.router.add_post("/lists/toggle", toggle)
    app.router.add_post("/lists/remove", remove)
    app.router.add_post("/lists/clear", clear)
    app.router.add_post("/lists/delete", delete)
    app.router.add_post("/memory/add", mem_add)
    app.router.add_post("/memory/remove", mem_remove)
