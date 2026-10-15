"""Understanding spoken durations ("an hour and a half") and times ("half past seven")."""
from __future__ import annotations

import datetime as dt
import re

_UNITS = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
}
_SMALL = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def words_to_numbers(text: str) -> str:
    """'twenty five minutes' -> '25 minutes', 'two and a half' -> '2.5'."""
    t = " " + text.lower().replace("-", " ") + " "
    t = re.sub(r"\b(a\.m\.?|a\. m\.?)", "am", t)
    t = re.sub(r"\b(p\.m\.?|p\. m\.?)", "pm", t)

    def tens(m):
        return f" {_TENS[m.group(1)] + (_SMALL[m.group(2)] if m.group(2) else 0)} "
    t = re.sub(r"\s(" + "|".join(_TENS) + r")(?:\s(" + "|".join(k for k in _SMALL if _SMALL[k] < 10 and k != 'zero') + r"))?(?=\s)", tens, t)
    t = re.sub(r"\s(" + "|".join(_SMALL) + r")(?=\s)", lambda m: f" {_SMALL[m.group(1)]} ", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\b(\d+) and a half\b", lambda m: f"{int(m.group(1)) + 0.5:g}", t)
    t = re.sub(r"\b(\d+) and a quarter\b", lambda m: f"{int(m.group(1)) + 0.25:g}", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_duration(text: str) -> int | None:
    """Total seconds mentioned in text, or None."""
    t = words_to_numbers(text)
    t = re.sub(r"\bhalf an? (hour|minute)\b", r"0.5 \1", t)
    t = re.sub(r"\ba quarter of an hour\b|\ba quarter hour\b", "15 minutes", t)
    t = re.sub(r"\b(an?|one) (hour|minute|second) and a half\b", r"1.5 \2", t)
    t = re.sub(r"\b(hour|minute) and a half\b", r"1.5 \1", t)
    t = re.sub(r"\b(an?)\s+(hour|minute|second)s?\b", r"1 \2", t)
    total = 0.0
    found = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b", t):
        total += float(num) * _UNITS[unit]
        found = True
    return int(round(total)) if found and total > 0 else None


def parse_clock(text: str, now: dt.datetime) -> tuple[dt.datetime, str] | None:
    """Find a clock time (plus optional day) in text. Returns (datetime, repeat).

    repeat is 'once', 'daily', 'weekdays' or 'weekends'.
    """
    t = words_to_numbers(text)
    repeat = "once"
    if re.search(r"\b(every ?day|daily|each day|every morning|every night)\b", t):
        repeat = "daily"
    elif re.search(r"\b(weekdays|every weekday|work ?days|monday to friday)\b", t):
        repeat = "weekdays"
    elif re.search(r"\b(weekends|every weekend)\b", t):
        repeat = "weekends"

    hour = minute = None
    ampm = None
    if re.search(r"\bnoon|midday\b", t):
        hour, minute, ampm = 12, 0, "pm"
    elif re.search(r"\bmidnight\b", t):
        hour, minute, ampm = 0, 0, "am"
    if hour is None:
        m = re.search(r"\b(half|quarter|\d{1,2})\s*(?:minutes?\s*)?(past|after|to|before)\s*(\d{1,2})\b(?:\s*(am|pm))?", t)
        if m:
            mins = {"half": 30, "quarter": 15}.get(m.group(1)) or int(m.group(1))
            h = int(m.group(3))
            if m.group(2) in ("to", "before"):
                h, mins = h - 1, 60 - mins
            hour, minute, ampm = h % 24, mins, m.group(4)
    if hour is None:
        m = re.search(r"\b(\d{1,2})(?:[:.](\d{2})|\s(\d{2})(?=\s*(?:am|pm)\b))?\s*(am|pm|o'?clock)?\b", t)
        # require some time-ish context so "add 2 eggs" isn't a time
        if m and (m.group(2) or m.group(3) or m.group(4) or re.search(r"\b(at|for|until|by)\s+\d", t)):
            hour = int(m.group(1))
            minute = int(m.group(2) or m.group(3) or 0)
            ampm = m.group(4) if m.group(4) in ("am", "pm") else None
    if hour is None or hour > 23 or minute > 59:
        return None

    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    elif ampm is None and hour <= 12:
        if re.search(r"\b(evening|tonight|afternoon|pm)\b", t) and hour < 12:
            hour += 12
        elif re.search(r"\bmorning\b", t) and hour == 12:
            hour = 0
        elif not re.search(r"\b(morning|am)\b", t):
            # ambiguous "7": pick whichever of 7:00 / 19:00 comes next
            cand_am = now.replace(hour=hour % 12, minute=minute, second=0, microsecond=0)
            cand_pm = cand_am + dt.timedelta(hours=12)
            options = [c for c in (cand_am, cand_pm, cand_am + dt.timedelta(days=1)) if c > now]
            if not re.search(r"\b(tomorrow|on \w+day)\b", t):
                hour = min(options).hour

    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    day_m = re.search(r"\b(today|tonight|tomorrow|" + "|".join(WEEKDAYS) + r")\b", t)
    if day_m and day_m.group(1) == "tomorrow":
        target = (now + dt.timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    elif day_m and day_m.group(1) in WEEKDAYS:
        delta = (WEEKDAYS.index(day_m.group(1)) - now.weekday()) % 7
        target = target + dt.timedelta(days=delta)
        if target <= now:
            target += dt.timedelta(days=7)
    elif target <= now:
        target += dt.timedelta(days=1)
    if repeat != "once":
        target = next_repeat(target - dt.timedelta(seconds=1), repeat, target.hour, target.minute)
    return target, repeat


def next_repeat(after: dt.datetime, repeat: str, hour: int, minute: int) -> dt.datetime:
    """Next time strictly after `after` that matches the repeat rule."""
    cand = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand <= after:
        cand += dt.timedelta(days=1)
    for _ in range(8):
        wd = cand.weekday()
        if repeat in ("once", "daily") or (repeat == "weekdays" and wd < 5) or (repeat == "weekends" and wd >= 5):
            return cand
        cand += dt.timedelta(days=1)
    return cand


# ---------------------------------------------------------------------------
# Saying things back
# ---------------------------------------------------------------------------
def say_duration(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if sec and not h and (m < 10):
        parts.append(f"{sec} second{'s' if sec != 1 else ''}")
    if not parts:
        return "less than a second"
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def say_clock(t: dt.datetime) -> str:
    h12 = t.hour % 12 or 12
    suffix = "AM" if t.hour < 12 else "PM"
    if t.hour == 12 and t.minute == 0:
        return "noon"
    if t.hour == 0 and t.minute == 0:
        return "midnight"
    return f"{h12} {suffix}" if t.minute == 0 else f"{h12}:{t.minute:02d} {suffix}"


def say_when(t: dt.datetime, now: dt.datetime) -> str:
    day = (t.date() - now.date()).days
    if day == 0:
        return f"at {say_clock(t)}"
    if day == 1:
        return f"tomorrow at {say_clock(t)}"
    if day < 7:
        return f"on {t.strftime('%A')} at {say_clock(t)}"
    return f"on {t.strftime('%A %d %B')} at {say_clock(t)}"
