"""The privacy wall: private data (your library, databases, memories) is only ever seen by the local model.

Anything private that goes into a prompt is wrapped with `mark()`. The Claude code path calls `for_claude()`
on everything it is about to send: conversation turns flagged private are dropped, and if marked text has
somehow got in anyway, it is cut out and logged as an error rather than sent.
"""
from __future__ import annotations

import logging

from . import config

log = logging.getLogger("privacy")

MARK = "<<PRIVATE: LOCAL MODEL ONLY>>"


def mark(text: str) -> str:
    return f"{MARK}\n{text}" if text else ""


def is_marked(text) -> bool:
    if isinstance(text, str):
        return MARK in text
    if isinstance(text, list):   # content blocks
        return any(is_marked(b.get("text", "")) for b in text if isinstance(b, dict))
    return False


def for_local(history: list[dict]) -> list[dict]:
    """The local model sees everything, minus our bookkeeping keys."""
    return [{"role": m["role"], "content": m["content"]} for m in history]


def for_claude(history: list[dict], extra: str) -> tuple[list[dict], str, int]:
    """What may leave the house: (messages, extra prompt, number of private turns held back)."""
    out: list[dict] = []
    held = 0
    for m in history:
        if m.get("private") or is_marked(m.get("content")):
            held += 1
            if is_marked(m.get("content")):
                log.error("Marked private text found in a conversation turn: kept it off Claude")
            continue
        out.append({"role": m["role"], "content": m["content"]})
    # dropping turns can leave two of the same role together, or an assistant turn first: tidy up
    tidy: list[dict] = []
    for m in out:
        if tidy and tidy[-1]["role"] == m["role"]:
            if m["role"] == "user":
                tidy[-1] = m          # keep the newer question
            continue                   # an assistant reply whose question was private
        tidy.append(m)
    while tidy and tidy[0]["role"] != "user":
        tidy.pop(0)
    if extra and is_marked(extra):
        log.error("Marked private text was about to go to Claude in the system prompt: removed")
        extra = ""
    return tidy, extra, held


def keep_memories_local() -> bool:
    return config.KEEP_PRIVATE_LOCAL
