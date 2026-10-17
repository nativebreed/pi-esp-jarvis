"""Screen modes the board draws itself: dice and the timer countdown.

The Pi only sends short commands through the board's "self.jarvis.screen" tool:
  dice        a, b, count   -> tumbling dice that land on a (and b)
  dice_off                  -> back to Visor
  timer       seconds, total, label, icon ("egg" | "timer") -> countdown ring
  timer_off / hide / show / off
Boards with older firmware simply don't have the tool; everything still works by voice.
"""
from __future__ import annotations

import re

_LEAD = r"^\W*(?:(?:hey|ok|okay|so|please|jarvis|can you|could you|would you|now|and)[\s,]+)*"

DICE_RE = re.compile(
    r"\b(?:roll|throw|toss|chuck|shake)\b.*?\b(?:dice|die|a d6)\b|"
    r"^\W*(?:(?:jarvis|ok|okay)[\s,]+)?(?:dice|die)(?: roll)?\W*$", re.I)
AGAIN_RE = re.compile(_LEAD + r"(?:roll|throw|go)\s+(?:it\s+|them\s+|the dice\s+)?again\b|" + _LEAD + r"re-?roll\b"
                      r"|" + _LEAD + r"(?:one more|another)(?:\s+(?:roll|go|throw))?\W*$", re.I)
TWO_RE = re.compile(r"\b(?:two|2|a pair of|pair of|both)\s+(?:dice|die)\b|\bdice\s*(?:x|times)\s*2\b", re.I)
FLIP_RE = re.compile(
    _LEAD + r"(?:(?:flip|turn|rotate|spin)\s+(?:the\s+|your\s+)?(?:screen|display|face|picture)(?:\s+(?:round|around|over|the other way|upside down))?|"
    r"(?:the\s+|your\s+)?(?:screen|display|face|picture)(?:'s|\s+is)\s+upside[\s-]down|you'?re\s+upside[\s-]down|"
    r"(?:fix|correct)\s+(?:the\s+)?(?:screen|display)(?:\s+orientation)?)\W*$", re.I)
DONE_RE = re.compile(_LEAD + r"(?:done|i'?m done|finished|that'?s it|stop|enough|thanks?|thank you|"
                     r"put (?:the|them|it) (?:dice )?away|no more|hide the dice|close the dice)\W*$", re.I)


def parse_dice(text: str, recent_count: int | None) -> int | None:
    """How many dice to roll (1 or 2), or None when this isn't a dice request.

    recent_count = how many dice were rolled in the last minute (for "roll again")."""
    t = text.strip()
    if DICE_RE.search(t):
        if TWO_RE.search(t):
            return 2
        if re.search(r"\b(?:one|a|1|single)\s+(?:dice|die)\b", t, re.I):
            return 1
        return recent_count or 1
    if recent_count and AGAIN_RE.match(t):
        return recent_count
    return None


def timer_caption(timer: dict, name: str) -> tuple[str, str]:
    """Caption under the countdown and its icon."""
    label = (timer.get("label") or "").strip()
    caption = label[:1].upper() + label[1:] if label else name[:1].upper() + name[1:]
    return caption, ("egg" if "egg" in label.lower() else "timer")
