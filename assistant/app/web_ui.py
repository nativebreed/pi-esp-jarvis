"""A small phone-friendly page for your lists and the things Jarvis remembers.

Open http://<pi>:8000/lists on any device on your home network.
"""
from __future__ import annotations

import html
import time

from aiohttp import web

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
</style></head>
<body><main>
<h1><span>●</span> Jarvis</h1>
{sections}
<section>
  <h2>New list</h2>
  <form class="add" method="post" action="/lists/add"><input name="list" placeholder="List name, e.g. holiday packing" required>
  <input type="hidden" name="text" value=""><button>Create</button></form>
</section>
<p class="small">Say "add milk to the shopping list", "remember the spare key is in the shed", or "what's on my to-do list".</p>
</main></body></html>"""


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


def add_routes(app: web.Application, store: Store) -> None:
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

    app.router.add_get("/lists", page)
    app.router.add_get("/", page)
    app.router.add_post("/lists/add", add)
    app.router.add_post("/lists/toggle", toggle)
    app.router.add_post("/lists/remove", remove)
    app.router.add_post("/lists/clear", clear)
    app.router.add_post("/lists/delete", delete)
    app.router.add_post("/memory/add", mem_add)
    app.router.add_post("/memory/remove", mem_remove)
