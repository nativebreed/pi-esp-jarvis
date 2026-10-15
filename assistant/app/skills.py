"""Everyday skills: lists, memory, weather, news, morning briefing, Pi control.

handle() looks at what the user said and returns a Reply, or None to let the
language models answer.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time

from . import config
from .clock import Clock
from .pihelper import PiHelper
from .reply import Reply
from .store import Store
from .weather import Weather, headlines

log = logging.getLogger("skills")

_LEAD = r"^\W*(?:(?:hey|ok|okay|so|please|jarvis|can you|could you|would you)[\s,]+)*"
_YES = re.compile(r"^\W*(yes|yeah|yep|yup|sure|do it|go ahead|please do|confirm|ok(ay)?|affirmative|absolutely|correct)\b", re.I)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


# ===========================================================================
LIST_ALIASES = {"grocery": "shopping", "groceries": "shopping", "shopping": "shopping",
                "food": "shopping", "todo": "to-do", "to do": "to-do", "to-do": "to-do",
                "task": "to-do", "tasks": "to-do", "things to do": "to-do"}
_LISTNAME = r"(?:the\s+|my\s+|our\s+)?(?P<list>[a-z][a-z\s'-]{0,30}?)\s*list"
ADD_RE = re.compile(_LEAD + r"(?:add|put|stick|pop|write)\s+(?P<items>.+?)\s+(?:on(?:to)?|to|in(?:to)?)\s+" + _LISTNAME + r"\W*$", re.I)
REMOVE_RE = re.compile(_LEAD + r"(?:remove|delete|take|cross|strike|tick)\s+(?P<items>.+?)\s+(?:off|from|out of)\s+" + _LISTNAME + r"\W*$", re.I)
CLEAR_RE = re.compile(_LEAD + r"(?:clear|empty|wipe|reset|delete)\s+(?:out\s+)?" + _LISTNAME + r"\W*$", re.I)
READ_RE = re.compile(r"\b(?:what'?s|what is|whats|read|tell me|show|check|go through|anything)\b.*?\b(?:on\s+|in\s+)?" + _LISTNAME + r"\b", re.I)

REMEMBER_RE = re.compile(_LEAD + r"remember(?:\s+that)?\s+(?P<what>.+?)\W*$", re.I)
RECALL_RE = re.compile(r"\bwhat (?:do|did) you (?:remember|know about me)\b|\bwhat have i (?:told|asked) you to remember\b|\bwhat are your memories\b", re.I)
FORGET_RE = re.compile(_LEAD + r"forget\s+(?:that\s+|about\s+)?(?P<what>.+?)\W*$", re.I)

WEATHER_RE = re.compile(r"\b(weather|forecast|raining|rain|rainy|snow|snowing|umbrella|jacket|coat|sunny|cloudy|windy|"
                        r"temperature (?:outside|today|tomorrow)|how (?:hot|cold|warm) is it|sunrise|sunset|uv index|"
                        r"(?:hot|cold|warm) (?:outside|today|tomorrow))\b", re.I)
GENERIC_WEATHER_RE = re.compile(r"^\W*(?:(?:hey|ok|okay|so|jarvis)[\s,]+)?(?:what'?s|what is|whats|how'?s|how is|tell me|give me)?\s*"
                                r"(?:the\s+)?(?:weather|forecast)(?:\s+(?:like|forecast|report))?"
                                r"(?:\s+(?:today|now|right now|outside|tomorrow|this week|for tomorrow|for today))?"
                                r"(?:\s+(?:in|for)\s+[\w .'-]+?)?(?:\s+(?:today|tomorrow|now))?\W*$", re.I)
NEWS_RE = re.compile(r"^\W*(?:(?:hey|ok|okay|so|jarvis)[\s,]+)?(?:what'?s|what is|whats|tell me|read(?: me)?|give me)?\s*(?:the\s+)?"
                     r"(?:latest\s+)?(?:news|headlines)(?:\s+(?:today|headlines|now|please))?\W*$", re.I)
BRIEFING_RE = re.compile(r"^\W*(?:(?:hey|ok|okay|so|jarvis)[\s,]+)?(?:good morning|morning briefing|daily briefing|"
                         r"(?:give me|start) (?:my|the) (?:morning |daily )?briefing|brief me|what'?s (?:my|the) day look(?:ing)? like|"
                         r"what does my day look like)(?:[\s,]+jarvis)?\W*$", re.I)
LIVE_RE = re.compile(r"\b(news (?:about|on)|latest|currently|right now|today'?s|tonight'?s|this week'?s|scores?|who won|won the|"
                     r"results? of|stock|share price|price of|exchange rate|bitcoin|open now|opening hours|traffic|election|"
                     r"search (?:the web|online|for)|look (?:it )?up|google|what happened|recent(?:ly)?|this year'?s|"
                     r"who is the (?:current )?(?:president|prime minister|ceo))\b", re.I)

PI_STATUS_RE = re.compile(r"\b(?:how'?s|how is|check(?: on)?|status of|health of)\b.*\b(?:the\s+)?(?:pi|raspberry pi|system|server|containers?)\b|"
                          r"\b(?:pi|raspberry pi|system)\s+(?:status|health|temperature|temp|stats)\b|"
                          r"\bhow (?:hot|warm) is the (?:pi|raspberry pi)\b|\bare the containers (?:running|ok|okay|up)\b", re.I)
OPEN_RE = re.compile(_LEAD + r"(?:open|launch|start|show me|bring up|go to|put on)\s+(?:the\s+|my\s+)?(?P<what>.+?)(?:\s+on the (?:screen|tv|pi|monitor))?\W*$", re.I)
CLOSE_RE = re.compile(_LEAD + r"(?:close|quit|exit|kill)\s+(?:the\s+|my\s+)?(?P<what>.+?)\W*$", re.I)
RUN_RE = re.compile(_LEAD + r"run\s+(?:the\s+|my\s+)?(?P<what>.+?)(?:\s+script)?\W*$", re.I)


class Skills:
    def __init__(self, store: Store, clock: Clock, weather: Weather, helper: PiHelper) -> None:
        self.store, self.clock, self.weather, self.helper = store, clock, weather, helper

    # -----------------------------------------------------------------------
    async def handle(self, text: str) -> Reply | None:
        low = text.lower().strip()
        if PI_STATUS_RE.search(low):
            return await self.pi_status()
        r = self.clock.handle(text)
        if r:
            return r
        r = self.lists(text)
        if r:
            return r
        r = self.memory(text)
        if r:
            return r
        if BRIEFING_RE.match(low):
            return Reply(await self.briefing(), emotion="happy")
        if NEWS_RE.match(low):
            return await self.news()
        if WEATHER_RE.search(low):
            return await self.weather_reply(text)
        r = await self.pi_control(text)
        if r:
            return r
        return None

    def wants_live_info(self, text: str) -> bool:
        return bool(LIVE_RE.search(text))

    # ======================================================================= lists
    @staticmethod
    def _list_name(raw: str) -> str:
        name = re.sub(r"\s+", " ", raw.lower()).strip(" '-")
        name = re.sub(r"^(?:the|my|our)\s+", "", name)
        return LIST_ALIASES.get(name, name)

    @staticmethod
    def _split_items(raw: str) -> list[str]:
        parts = re.split(r",\s*|\s+and\s+|\s*&\s*|\s+plus\s+", raw.strip(" ."))
        out = []
        for p in parts:
            p = re.sub(r"^(?:some|a|an|the|more|a few|a couple of)\s+", "", p.strip(), flags=re.I).strip(" .")
            if p:
                out.append(p)
        return out

    def lists(self, text: str) -> Reply | None:
        t = text.strip()
        m = ADD_RE.match(t)
        if m:
            name, items = self._list_name(m.group("list")), self._split_items(m.group("items"))
            added = self.store.list_add(name, items)
            if not added:
                return Reply(f"{_join(items).capitalize()} {'is' if len(items) == 1 else 'are'} already on your {name} list.")
            return Reply(f"Added {_join(added)} to your {name} list.")
        m = REMOVE_RE.match(t)
        if m:
            name, items = self._list_name(m.group("list")), self._split_items(m.group("items"))
            removed = self.store.list_remove(name, items)
            if not removed:
                return Reply(f"I couldn't find {_join(items)} on your {name} list.")
            return Reply(f"Removed {_join(removed)} from your {name} list.")
        m = CLEAR_RE.match(t)
        if m:
            name = self._list_name(m.group("list"))
            n = self.store.list_clear(name)
            return Reply(f"Cleared your {name} list." if n else f"Your {name} list was already empty.")
        m = READ_RE.search(t)
        if m and not re.search(r"\b(remind|timer|alarm|play)\b", t, re.I):
            name = self._list_name(m.group("list"))
            items = [i["text"] for i in self.store.list_items(name) if not i.get("done")]
            if not items:
                return Reply(f"Your {name} list is empty.")
            return Reply(f"You have {_plural(len(items), 'thing')} on your {name} list: {_join(items)}.")
        if re.search(r"\bwhat lists\b|\bwhich lists\b", t, re.I):
            names = [n for n in self.store.list_names()]
            return Reply(f"You have these lists: {_join(names)}." if names else "You don't have any lists yet.")
        return None

    # ====================================================================== memory
    def memory(self, text: str) -> Reply | None:
        t = text.strip()
        if RECALL_RE.search(t):
            mems = [m["text"] for m in self.store.items("memories")]
            if not mems:
                return Reply("You haven't asked me to remember anything yet.")
            shown = mems[-8:]
            return Reply(f"You asked me to remember {_plural(len(mems), 'thing')}. " + " ".join(
                (m if m.endswith(".") else m + ".") for m in shown))
        m = FORGET_RE.match(t)
        if m:
            what = m.group("what").lower()
            if re.match(r"^(everything|it all|all of it|all memories|all of that)$", what):
                return Reply("Are you sure you want me to forget everything you've asked me to remember?",
                             expect=self._confirm_forget_all)
            words = {w for w in re.findall(r"[a-z0-9']{3,}", what)} - {"that", "the", "about", "what", "said"}
            best, score = None, 0
            for mem in self.store.items("memories"):
                s = len(words & set(re.findall(r"[a-z0-9']{3,}", mem["text"].lower())))
                if s > score:
                    best, score = mem, s
            if not best:
                return Reply("I couldn't find anything like that in what you asked me to remember.")
            self.store.remove("memories", {best["id"]})
            return Reply("OK, I've forgotten that.")
        m = REMEMBER_RE.match(t)
        if m and not re.match(r"^\W*do you remember\b", t, re.I):
            what = m.group("what").strip()
            if re.match(r"^to\s+", what, re.I):  # "remember to buy milk" -> to-do list
                item = re.sub(r"^to\s+", "", what, flags=re.I)
                self.store.list_add("to-do", [item])
                return Reply(f"Added {item} to your to-do list.")
            what = re.sub(r"\bmy\b", "your", what, flags=re.I)
            what = re.sub(r"\bI am\b|\bI'm\b", "you're", what)
            what = re.sub(r"\bI\b", "you", what)
            what = re.sub(r"\bme\b", "you", what, flags=re.I)
            fact = what[0].upper() + what[1:]
            self.store.add("memories", {"text": fact, "created": time.time()})
            return Reply("Got it, I'll remember that.")
        return None

    async def _confirm_forget_all(self, text: str) -> Reply | None:
        if _YES.match(text):
            mems = self.store.items("memories")
            self.store.remove("memories", {m["id"] for m in mems})
            return Reply("Done. I've forgotten everything you asked me to remember.")
        return Reply("OK, I'll keep everything.")

    def memory_prompt(self) -> str:
        mems = [m["text"] for m in self.store.items("memories")][-30:]
        if not mems:
            return ""
        return "Things the user has asked you to remember:\n- " + "\n- ".join(mems)

    # ===================================================================== weather
    async def weather_reply(self, text: str) -> Reply:
        place = None
        m = re.search(r"\b(?:in|for|at)\s+([A-Z][\w'-]*(?:[\s-][A-Z][\w'-]*)*(?:,\s*[A-Z][\w ]+)?)", text)
        if m and m.group(1).lower() not in {"the", "today", "tomorrow"}:
            place = await self.weather.geocode(m.group(1))
        if not place:
            place = await self.weather.home()
        if not place:
            return Reply("I don't know where you are. Set WEATHER_LOCATION in the settings.")
        data = await self.weather.forecast(place)
        if not data:
            return Reply("Sorry, I couldn't get the weather right now.")
        low = text.lower()
        day = 1 if "tomorrow" in low else 0
        for i, name in enumerate(data["daily"]["time"]):
            wd = dt.date.fromisoformat(name).strftime("%A").lower()
            if i > 1 and wd in low:
                day = i
        if GENERIC_WEATHER_RE.match(low):
            return Reply(self.weather.spoken(place, data, day), emotion="happy")
        return Reply(llm_context="Current weather data (use it to answer; give temperatures as plain numbers):\n"
                                 + self.weather.as_context(place, data),
                     question=text, text=self.weather.spoken(place, data, day))

    # ======================================================================== news
    async def news(self) -> Reply:
        titles = await headlines(5)
        if not titles:
            return Reply("Sorry, I couldn't get the news right now.")
        return Reply("Here are the latest headlines. " + " ".join(t.rstrip(".") + "." for t in titles))

    # ==================================================================== briefing
    async def briefing(self) -> str:
        now = dt.datetime.now().astimezone()
        part = "morning" if now.hour < 12 else "afternoon" if now.hour < 18 else "evening"
        bits = [f"Good {part}! It's {now.strftime('%A')} the {_ordinal(now.day)} of {now.strftime('%B')}, "
                f"and the time is {now.strftime('%-I:%M %p').replace('AM', 'AM').replace('PM', 'PM')}."]
        place = await self.weather.home()
        if place:
            data = await self.weather.forecast(place)
            if data:
                bits.append(self.weather.spoken(place, data, 0))
        today = self.clock.today_summary()
        if today:
            bits.append("Today you have " + _join(today) + ".")
        nxt = self.clock.next_alarm()
        if nxt and now.hour >= 12:
            bits.append(f"Your next alarm is {nxt}.")
        shopping = [i for i in self.store.list_items("shopping") if not i.get("done")]
        todo = [i for i in self.store.list_items("to-do") if not i.get("done")]
        if todo:
            bits.append(f"Your to-do list has {_plural(len(todo), 'item')}.")
        if shopping:
            bits.append(f"And there {'is' if len(shopping) == 1 else 'are'} {_plural(len(shopping), 'thing')} on your shopping list.")
        titles = await headlines(3)
        if titles:
            bits.append("In the news: " + " ".join(t.rstrip(".") + "." for t in titles))
        bits.append("Have a great day!" if part == "morning" else "That's everything.")
        return " ".join(bits)

    # =================================================================== the Pi
    async def pi_status(self) -> Reply:
        if not self.helper.configured:
            return Reply("I can't check the Pi yet. Run the Pi helper installer first; it's explained in the README.")
        try:
            s = await self.helper.status()
        except Exception as exc:
            log.warning("Pi helper unreachable: %s", exc)
            return Reply("I can't reach the Pi helper service. Check that it's running.")
        bits = []
        if s.get("temperature") is not None:
            t = round(s["temperature"])
            bits.append(f"The Pi is at {t} degrees" + (", which is running hot" if t >= 75 else ""))
        if s.get("cpu_percent") is not None:
            bits.append(f"the processor is {round(s['cpu_percent'])} percent busy")
        if s.get("memory_percent") is not None:
            bits.append(f"memory is {round(s['memory_percent'])} percent used")
        if s.get("disk_percent") is not None:
            bits.append(f"the disk is {round(s['disk_percent'])} percent full")
        out = (_join(bits) if bits else "The Pi is running") + "."
        out = out[0].upper() + out[1:]
        conts = s.get("containers") or []
        if conts:
            bad = [c["name"] for c in conts if not c.get("healthy", True)]
            out += (f" All {_plural(len(conts), 'container')} are running fine." if not bad
                    else f" Warning: {_join(bad)} {'is' if len(bad) == 1 else 'are'} not healthy.")
        if s.get("throttled"):
            out += " It has been slowing itself down because of heat or a weak power supply."
        if s.get("uptime_s") and s["uptime_s"] >= 3600:
            days = int(s["uptime_s"] // 86400)
            hours = int(s["uptime_s"] % 86400 // 3600)
            out += f" It's been up for {_plural(days, 'day') + ' and ' if days else ''}{_plural(hours, 'hour')}."
        return Reply(out, emotion="cool" if not s.get("throttled") else "sad")

    async def pi_control(self, text: str) -> Reply | None:
        if not self.helper.configured:
            if OPEN_RE.match(text) or RUN_RE.match(text) or re.search(r"\b(reboot|shut ?down)\b", text, re.I):
                return Reply("I can't control the Pi yet. Run the Pi helper installer first; it's explained in the README.")
            return None
        try:
            cat = await self.helper.catalog()
        except Exception:
            return None
        low = text.lower().strip(" .!?")
        # named actions ("reboot the pi", "run the backup")
        for act in cat.get("actions", []):
            phrases = [act["name"].lower()] + [p.lower() for p in act.get("phrases", [])]
            hit = any(re.search(rf"\b{re.escape(p)}\b", low) for p in act.get("phrases", []))
            m = RUN_RE.match(text)
            if m and m.group("what").lower() in phrases:
                hit = True
            if hit:
                return await self._action(act)
        m = CLOSE_RE.match(text)
        if m:
            app = self._find_app(cat, m.group("what"))
            if app:
                return await self._run(app_close=app)
        m = OPEN_RE.match(text)
        if m:
            what = m.group("what").lower().strip()
            if re.search(r"\b(timer|alarm|list|music|radio|reminder)\b", what):
                return None
            app = self._find_app(cat, what)
            if app:
                return await self._run(app_open=app)
            sites = {k.lower(): v for k, v in cat.get("websites", {}).items()}
            url = sites.get(what) or sites.get(re.sub(r"\s+", "", what))
            if not url and re.match(r"^[a-z0-9-]+(\s?dot\s?|\.)[a-z.]{2,}$", what):
                url = "https://" + re.sub(r"\s?dot\s?", ".", what)
            if url:
                return await self._run(url=url, label=what)
            return Reply(f"I don't know how to open {what} yet. You can add it to the Pi helper's actions file.")
        m = RUN_RE.match(text)
        if m:
            return Reply(f"I don't have an action called {m.group('what')}. You can add it to the Pi helper's actions file.")
        return None

    @staticmethod
    def _find_app(cat: dict, what: str) -> str | None:
        w = what.lower().strip()
        for name, app in cat.get("apps", {}).items():
            if w == name.lower() or w in [a.lower() for a in app.get("aliases", [])]:
                return name
        return None

    async def _action(self, act: dict) -> Reply:
        say = act.get("say") or act["name"]
        if act.get("confirm"):
            async def confirm(text: str) -> Reply | None:
                if _YES.match(text):
                    return await self._run(action=act)
                return Reply("OK, I won't.")
            return Reply(f"Are you sure you want me to {say}?", emotion="confused", expect=confirm)
        return await self._run(action=act)

    async def _run(self, action: dict | None = None, app_open: str | None = None,
                   app_close: str | None = None, url: str | None = None, label: str = "") -> Reply:
        try:
            if action:
                res = await self.helper.run(action["name"])
                ok_text = action.get("done") or f"Done. I've started {action.get('say') or action['name']}."
            elif app_open:
                res = await self.helper.run("open_app", app_open)
                ok_text = f"Opening {app_open}."
            elif app_close:
                res = await self.helper.run("close_app", app_close)
                ok_text = f"Closed {app_close}."
            else:
                res = await self.helper.run("open_url", url)
                ok_text = f"Opening {label or url} on the Pi."
        except Exception as exc:
            log.warning("Pi helper call failed: %s", exc)
            return Reply("Sorry, the Pi helper didn't respond.", emotion="sad")
        if not res.get("ok"):
            return Reply(f"That didn't work. {res.get('error', '')}".strip(), emotion="sad")
        if res.get("output") and action and not action.get("background"):
            out = res["output"].strip().splitlines()[-1][:150]
            return Reply(f"{ok_text} It said: {out}")
        return Reply(ok_text, end=bool(action and action.get("ends_session")))
