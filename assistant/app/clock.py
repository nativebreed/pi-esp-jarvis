"""Timers, alarms and reminders: understanding the request, keeping time, raising alerts."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from .reply import Reply
from .store import Store
from .timeparse import (next_repeat, parse_clock, parse_duration, say_clock, say_duration,
                        say_when, words_to_numbers)

log = logging.getLogger("clock")

SNOOZE_MIN = 9
MAX_AUTO_SNOOZES = 3


@dataclass
class Alert:
    kind: str            # timer | alarm | reminder | announce
    text: str            # what to say
    item: dict = field(default_factory=dict)
    created: float = field(default_factory=time.time)


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _ts(d: dt.datetime) -> float:
    return d.timestamp()


def _from_ts(t: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(t).astimezone()


_CANCEL = r"\b(cancel|stop|delete|remove|clear|turn off|switch off|disable|end|get rid of)\b"
_QUERY = r"\b(how (much|long)|what|which|when|any|do i have|list|tell me|check|remaining|left)\b"


class Clock:
    def __init__(self, store: Store, on_alert: Callable[[Alert], None]) -> None:
        self.store = store
        self.on_alert = on_alert

    # ======================================================== keeping time
    async def run(self) -> None:
        self._catch_up()
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("clock tick failed")
            await asyncio.sleep(1)

    def _catch_up(self) -> None:
        """After a restart: drop stale timers/alarms, move repeating alarms forward."""
        now = time.time()
        for t in self.store.items("timers"):
            if t["due"] < now - 900:
                self.store.remove("timers", {t["id"]})
        for a in self.store.items("alarms"):
            if a["due"] < now - 900:
                if a.get("repeat", "once") == "once":
                    self.store.remove("alarms", {a["id"]})
                else:
                    nxt = next_repeat(_now(), a["repeat"], *map(int, a["time"].split(":")))
                    self.store.update("alarms", a["id"], due=_ts(nxt))

    def tick(self) -> None:
        now = time.time()
        for t in self.store.items("timers"):
            if t["due"] <= now:
                self.store.remove("timers", {t["id"]})
                label = t.get("label") or ""
                what = f"Your {label} timer is done." if label else \
                    f"Time's up! Your {self._timer_name(t)} is done."
                self.on_alert(Alert("timer", what, t))
        for a in self.store.items("alarms"):
            if a["due"] <= now:
                if a.get("repeat", "once") == "once":
                    self.store.remove("alarms", {a["id"]})
                else:
                    nxt = next_repeat(_now() + dt.timedelta(minutes=1), a["repeat"],
                                      *map(int, a["time"].split(":")))
                    self.store.update("alarms", a["id"], due=_ts(nxt), snoozes=0)
                hour = _now().hour
                if a.get("label"):
                    what = f"It's {say_clock(_now())}. {a['label'].capitalize()}."
                elif 4 <= hour < 11:
                    what = f"Good morning! It's {say_clock(_now())}. Time to wake up."
                else:
                    what = f"It's {say_clock(_now())}. This is your alarm."
                self.on_alert(Alert("alarm", what, a))
        for r in self.store.items("reminders"):
            if r["due"] <= now:
                self.store.remove("reminders", {r["id"]})
                late = now - r["due"] > 900
                if now - r["due"] < 12 * 3600:
                    prefix = "Reminder from earlier" if late else "Reminder"
                    self.on_alert(Alert("reminder", f"{prefix}: {r['text']}.", r))

    # ======================================================== alert outcomes
    def snooze(self, alert: Alert, minutes: int = SNOOZE_MIN, auto: bool = False) -> str:
        due = _now() + dt.timedelta(minutes=minutes)
        snoozes = alert.item.get("snoozes", 0) + 1
        if auto and snoozes > MAX_AUTO_SNOOZES:
            return ""
        self.store.add("alarms", {
            "label": alert.item.get("label", ""), "time": due.strftime("%H:%M"),
            "repeat": "once", "due": _ts(due), "snoozes": snoozes, "snoozed": True})
        return f"Snoozing for {minutes} minutes."

    # ======================================================== understanding requests
    def handle(self, text: str) -> Reply | None:
        low = text.lower()
        egg = self._egg(text, low)
        if egg:
            return egg
        if re.search(r"\btimers?\b", low) or (re.search(r"\b(how (much time|long)( is)? left|time left)\b", low)
                                              and self.store.items("timers")):
            return self._timers(text, low)
        if re.search(r"\balarms?\b|\bwake me\b|\bwake-up\b", low):
            return self._alarms(text, low)
        if re.search(r"^\W*(?:(?:hey|ok|okay|please|jarvis|can you|could you)[\s,]+)*remind me\b", low) \
                or re.search(r"\breminders?\b", low):
            return self._reminders(text, low)
        if re.search(r"^\W*snooze\b", low):
            return Reply("There's nothing to snooze right now.")
        return None

    # ---------------------------------------------------------------- timers
    def _timers(self, text: str, low: str) -> Reply:
        timers = sorted(self.store.items("timers"), key=lambda t: t["due"])
        if re.search(_CANCEL, low):
            label = self._label_match(low, timers)
            if label is not None:
                gone = [t for t in timers if (t.get("label") or "") == label]
            elif re.search(r"\b(all|every)\b", low) or len(timers) <= 1:
                gone = timers
            else:
                return Reply(f"You have {len(timers)} timers: {self._timer_list(timers)}. "
                             "Which one should I cancel? Or say cancel all timers.")
            if not gone:
                return Reply("You don't have any timers running.")
            self.store.remove("timers", {t["id"] for t in gone})
            return Reply("Cancelled all your timers." if len(gone) > 1 else
                         f"Cancelled your {self._timer_name(gone[0])}.")
        seconds = parse_duration(text)
        if seconds and not re.search(r"\b(how (much|long)|left|remaining)\b", low):
            label = self._timer_label(low)
            due = time.time() + seconds
            self.store.add("timers", {"label": label, "due": due, "duration": seconds})
            name = f"{label} timer" if label else "timer"
            return Reply(f"OK, {name} set for {say_duration(seconds)}.", emotion="timer")
        if not timers:
            return Reply("You don't have any timers running.")
        return Reply(self._timer_list(timers, with_left=True).capitalize() + ".")

    # Egg timer presets: "boil an egg", "soft-boiled egg timer", "hard boiled eggs"
    _EGG_RE = re.compile(r"\b(?:(?P<kind>soft|medium|hard|runny|dippy|jammy)[\s-]*boiled\s+eggs?|"
                         r"boil(?:ing)?\s+(?:an?\s+|some\s+|two\s+|three\s+|four\s+|the\s+|my\s+)?eggs?|"
                         r"eggs?\s+timer|(?P<kind2>soft|medium|hard|runny|dippy|jammy)\s+eggs?)\b")
    _EGG_TIMES = {"soft": 360, "runny": 300, "dippy": 300, "jammy": 420, "medium": 480, "hard": 600}

    def _egg(self, text: str, low: str) -> Reply | None:
        m = self._EGG_RE.search(low)
        if not m or re.search(_CANCEL, low) or re.search(r"\b(how (much|long)|left|remaining)\b", low):
            return None
        kind = m.group("kind") or m.group("kind2") or ""
        seconds = parse_duration(text) or self._EGG_TIMES.get(kind, 360)
        label = f"{kind}-boiled egg" if kind else "egg"
        self.store.add("timers", {"label": label, "due": time.time() + seconds, "duration": seconds})
        msg = f"OK, {label} timer set for {say_duration(seconds)}."
        if not kind and not parse_duration(text):
            msg += " That's soft-boiled. Say hard-boiled egg for ten minutes."
        return Reply(msg, emotion="timer")

    @staticmethod
    def _timer_label(low: str) -> str:
        m = re.search(r"\b(?:an?|the|my)\s+(?!\d)([a-z][a-z ]{1,24}?)\s+timer\b", low)
        if m and not re.search(r"\b(minute|second|hour|half)s?\b", m.group(1)):
            return m.group(1).strip()
        m = re.search(r"\btimer\b.*?\b(?:for|called|named)\s+(?:the\s+|my\s+)?([a-z][a-z ]{1,24}?)\W*$", low)
        if m and not re.search(r"\d|\b(minute|second|hour|half|an?)\b", m.group(1)):
            return m.group(1).strip()
        return ""

    @staticmethod
    def _label_match(low: str, items: list[dict]) -> str | None:
        low = low.replace("-", " ")
        for it in sorted(items, key=lambda i: -len(i.get("label") or "")):  # longest name first
            label = (it.get("label") or "").lower().replace("-", " ")
            if label and label in low:
                return it.get("label")
        words = set(re.findall(r"[a-z]+", low))
        for it in items:  # "cancel the egg timer" finds "soft-boiled egg"
            last = (it.get("label") or "").lower().split(" ")[-1]
            if len(last) >= 3 and last in words:
                return it.get("label")
        return None

    @staticmethod
    def _timer_name(t: dict) -> str:
        if t.get("label"):
            return f"{t['label']} timer"
        adj = say_duration(t["duration"])
        for unit in ("hour", "minute", "second"):
            adj = adj.replace(f" {unit}s", f" {unit}")
        return f"{adj} timer"

    def _timer_list(self, timers: list[dict], with_left: bool = False) -> str:
        now = time.time()
        parts = []
        for t in timers:
            left = say_duration(max(0, t["due"] - now))
            parts.append(f"{left} left on your {self._timer_name(t)}" if with_left else self._timer_name(t))
        return ", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0]

    # ---------------------------------------------------------------- alarms
    def _alarms(self, text: str, low: str) -> Reply:
        now = _now()
        alarms = sorted(self.store.items("alarms"), key=lambda a: a["due"])
        if re.search(_CANCEL, low):
            parsed = parse_clock(text, now)
            if parsed:
                hm = parsed[0].strftime("%H:%M")
                gone = [a for a in alarms if a["time"] == hm]
            elif re.search(r"\b(all|every)\b", low) or len(alarms) <= 1:
                gone = alarms
            else:
                return Reply(f"You have {len(alarms)} alarms: {self._alarm_list(alarms, now)}. "
                             "Which one should I turn off? Or say cancel all alarms.")
            if not gone:
                return Reply("I couldn't find that alarm." if alarms else "You don't have any alarms set.")
            self.store.remove("alarms", {a["id"] for a in gone})
            return Reply("Turned off all your alarms." if len(gone) > 1 else
                         f"Turned off your {say_clock(_from_ts(gone[0]['due']))} alarm.")
        clock_text = text
        if (re.search(r"\bwake\b", low) or re.search(r"\b(every|daily|weekdays?|weekends?)\b", low)) \
                and not re.search(r"\b(pm|p\.m\.|evening|tonight|afternoon|night)\b", low):
            clock_text = text + " in the morning"   # wake-up alarms default to AM
        parsed = None if re.search(r"\b(what|which|when|any|list|do i have)\b", low) else parse_clock(clock_text, now)
        if parsed:
            due, repeat = parsed
            label = ""
            m = re.search(r"\b(?:to|for)\s+(?!\d)([a-z][a-z ]{2,40}?)\W*$", low)
            if m and not re.search(r"\b(am|pm|morning|evening|tomorrow|tonight|today|day|weekdays?|weekends?|o'?clock|noon|midnight)\b", m.group(1)):
                label = m.group(1).strip()
            self.store.add("alarms", {"label": label, "time": due.strftime("%H:%M"),
                                      "repeat": repeat, "due": _ts(due), "snoozes": 0})
            rep = {"once": "", "daily": " every day", "weekdays": " on weekdays",
                   "weekends": " at weekends"}[repeat]
            days = (due.date() - now.date()).days
            day = "" if repeat != "once" or days == 0 else " tomorrow" if days == 1 else f" on {due.strftime('%A')}"
            your_label = re.sub(r"\bmy\b", "your", label)
            purpose = f" to {your_label}" if label else ""
            msg = f"Alarm set for {say_clock(due)}{day}{rep}{purpose}."
            if repeat == "once" and due.timestamp() - now.timestamp() < 86400:
                msg += f" That's {say_duration(due.timestamp() - now.timestamp())} from now."
            return Reply(msg, emotion="timer")
        if re.search(r"\b(alarm|wake me)\b", low) and not re.search(_QUERY, low):
            return Reply("What time should I set the alarm for?",
                         expect=lambda t: self._alarm_followup(t))
        if not alarms:
            return Reply("You don't have any alarms set.")
        return Reply(f"You have {len(alarms)} alarm{'s' if len(alarms) > 1 else ''}: "
                     f"{self._alarm_list(alarms, now)}.")

    async def _alarm_followup(self, text: str) -> Reply | None:
        if parse_clock(text, _now()):
            return self._alarms("set an alarm for " + text, "set an alarm for " + text.lower())
        return None

    @staticmethod
    def _alarm_list(alarms: list[dict], now: dt.datetime) -> str:
        parts = []
        for a in alarms:
            due = _from_ts(a["due"])
            rep = a.get("repeat", "once")
            if rep == "once":
                parts.append(say_when(due, now).removeprefix("at "))
            else:
                parts.append(f"{say_clock(due)} " + {"daily": "every day", "weekdays": "on weekdays",
                                                     "weekends": "at weekends"}[rep])
        return ", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0]

    # ------------------------------------------------------------- reminders
    def _reminders(self, text: str, low: str) -> Reply:
        now = _now()
        reminders = sorted(self.store.items("reminders"), key=lambda r: r["due"])
        if not re.search(r"remind me\b", low):
            if re.search(_CANCEL, low):
                words = [w for w in re.findall(r"[a-z']{4,}", low)
                         if w not in {"cancel", "delete", "remove", "clear", "reminder", "reminders", "about", "that"}]
                gone = [r for r in reminders if any(w in r["text"].lower() for w in words)] if words else []
                if not gone and (re.search(r"\b(all|every)\b", low) or len(reminders) == 1):
                    gone = reminders
                if not gone:
                    return Reply("You don't have any reminders." if not reminders else
                                 "Which reminder? Say cancel the reminder about, and then what it's about.")
                self.store.remove("reminders", {r["id"] for r in gone})
                return Reply("Deleted all your reminders." if len(gone) > 1 else
                             f"Deleted the reminder to {gone[0]['text']}.")
            if not reminders:
                return Reply("You don't have any reminders.")
            parts = [f"{r['text']} {say_when(_from_ts(r['due']), now)}" for r in reminders[:5]]
            return Reply(f"You have {len(reminders)} reminder{'s' if len(reminders) > 1 else ''}: "
                         + "; ".join(parts) + ".")

        due = self._reminder_due(text, now)
        what = self._reminder_text(text)
        if not what:
            return Reply("What should I remind you about?",
                         expect=lambda t: self._reminder_followup(t, due, None))
        if not due:
            return Reply(f"When should I remind you to {what}?",
                         expect=lambda t: self._reminder_followup(t, None, what))
        return self._save_reminder(what, due, now)

    async def _reminder_followup(self, text: str, due, what) -> Reply | None:
        now = _now()
        if what is None:
            what = self._reminder_text("remind me to " + text) or text.strip(" .")
        if due is None:
            due = self._reminder_due(text, now)
            if not due:
                return None
        return self._save_reminder(what, due, now)

    def _save_reminder(self, what: str, due: dt.datetime, now: dt.datetime) -> Reply:
        self.store.add("reminders", {"text": what, "due": _ts(due)})
        if (due - now).total_seconds() < 3 * 3600:
            when = "in " + say_duration((due - now).total_seconds())
        else:
            when = say_when(due, now)
        return Reply(f"OK, I'll remind you to {what} {when}.", emotion="timer")

    @staticmethod
    def _reminder_due(text: str, now: dt.datetime) -> dt.datetime | None:
        w = words_to_numbers(text)
        m = re.search(r"\bin\s+((?:an?|half|\d)[^,]*?(?:seconds?|minutes?|mins?|hours?|hrs?)(?:\s+and\s+a\s+half)?)\b", w)
        if m:
            secs = parse_duration(m.group(1))
            if secs:
                return now + dt.timedelta(seconds=secs)
        parsed = parse_clock(text, now)
        if parsed:
            return parsed[0]
        day = re.search(r"\b(tomorrow|tonight|this (morning|afternoon|evening)|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", w)
        if day:  # a day but no time: pick a sensible hour
            hour = {"tonight": 20, "this morning": 9, "this afternoon": 15, "this evening": 19}.get(day.group(1), 9)
            parsed = parse_clock(f"at {hour}:00 {day.group(1) if day.group(1) in ('tomorrow',) or day.group(1).endswith('day') else ''}", now)
            return parsed[0] if parsed else None
        return None

    @staticmethod
    def _reminder_text(text: str) -> str:
        t = " " + text.strip().rstrip(".!?") + " "
        t = re.sub(r"^\W*(?:(?:hey|ok|okay|please|jarvis|can you|could you)[\s,]+)*remind me\b", " ", t, flags=re.I)
        patterns = [
            r"\bin\s+(?:about\s+)?(?:an?|half|a couple of|\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty|forty five|sixty|ninety)\b[\w\s]*?\b(?:seconds?|minutes?|mins?|hours?|hrs?)(?:\s+and\s+a\s+half)?\b",
            r"\b(?:at|by|around)\s+(?:half past|quarter (?:past|to)|\d{1,2}(?:[:.]\d{2})?|noon|midnight|(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)(?:\s+(?:fifteen|thirty|forty five))?)(?:\s*(?:a\.?m\.?|p\.?m\.?|o'?clock))?(?:\s+(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d{1,2}))?\b",
            r"\b(?:today|tonight|tomorrow(?:\s+(?:morning|afternoon|evening|night))?|this (?:morning|afternoon|evening)|in the (?:morning|afternoon|evening)|on (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
        ]
        for p in patterns:
            t = re.sub(p, " ", t, flags=re.I)
        t = re.sub(r"\s+", " ", t).strip(" ,.")
        t = re.sub(r"^(?:to|that|about)\s+", "", t, flags=re.I)
        t = re.sub(r"\s+(?:to|that)$", "", t, flags=re.I)
        t = re.sub(r"\bmy\b", "your", t, flags=re.I)
        t = re.sub(r"\bI\b", "you", t)
        return t.strip()

    # ======================================================== for the briefing
    def today_summary(self) -> list[str]:
        now = _now()
        end = now.replace(hour=23, minute=59, second=59)
        out = []
        for r in sorted(self.store.items("reminders"), key=lambda r: r["due"]):
            due = _from_ts(r["due"])
            if due <= end:
                out.append(f"a reminder to {r['text']} at {say_clock(due)}")
        timers = self.store.items("timers")
        if timers:
            out.append(self._timer_list(timers, with_left=True))
        return out

    def next_alarm(self) -> str | None:
        alarms = sorted(self.store.items("alarms"), key=lambda a: a["due"])
        if not alarms:
            return None
        return say_when(_from_ts(alarms[0]["due"]), _now()).removeprefix("at ")
