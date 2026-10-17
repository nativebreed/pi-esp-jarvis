"""Decides who answers (local model or Claude) and streams the reply text."""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx

from . import config

log = logging.getLogger("brain")

# "ask Claude ..." / "use Claude ..." — Whisper often hears "Claude" as cloud/Clyde/clawed, so accept those too.
_CLAUDE_WORD = r"(?:claude|claud|clawed|clod|clode|cloud|clyde|klaud|klod)"
TRIGGER_RE = re.compile(
    rf"^\W*(?:(?:hey|ok|okay|so|um|uh)[\s,]+)?(?:please\s+)?(?:can you\s+|could you\s+)?"
    rf"(?:ask|use|using)\s+{_CLAUDE_WORD}\b[\s,.:;!?-]*"
    rf"(?:(?:to|for|and|about)\b[\s,]*)?(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)
GOODBYE_RE = re.compile(
    r"^\W*(?:ok(?:ay)?[\s,]+)?(?:good\s?bye|bye(?:\s+bye)?|see you(?: later)?|that'?s all|"
    r"stop listening|go to sleep|never mind|nevermind)(?:[\s,]+(?:thanks|thank you|jarvis))*\W*$",
    re.IGNORECASE,
)


def match_claude_trigger(text: str) -> str | None:
    """Return the question after 'ask/use Claude' (possibly ''), or None if not triggered."""
    m = TRIGGER_RE.match(text.strip())
    if not m:
        return None
    return m.group("rest").strip()


def is_goodbye(text: str) -> bool:
    return bool(GOODBYE_RE.match(text.strip()))


def _now() -> str:
    return dt.datetime.now().astimezone().strftime("%A %d %B %Y, %H:%M %Z")


def local_system_prompt() -> str:
    return (
        f"You are {config.ASSISTANT_NAME}, a friendly voice assistant running on a small "
        f"home device. The current date and time is {_now()}. Your replies are spoken aloud, "
        "so answer in one to three short, natural sentences. Never use markdown, lists, "
        "emoji or code. Other parts of the system handle music, timers, alarms, reminders, "
        "lists, weather and controlling the Pi; you cannot do those things yourself and must "
        "never pretend to. If the user asks for one of them in a way you received, tell them "
        "the simple phrase to use, for example 'set a timer for 10 minutes', 'add milk to the "
        "shopping list' or 'play jazz'. If a question needs deep knowledge, careful reasoning "
        "or up-to-date facts, give your best short answer and suggest the user say 'use Claude'."
    )


def claude_system_prompt() -> str:
    return (
        f"You are Claude, answering through a voice assistant speaker called "
        f"{config.ASSISTANT_NAME}. The current date and time is {_now()}. Your answer will be "
        "read aloud by a text-to-speech voice, so write plain conversational sentences: no "
        "markdown, bullet points, headings, tables, code blocks, URLs or emoji. Be concise — "
        "usually under 120 words — unless the user explicitly asks for detail. Spell out "
        "symbols and abbreviations the way they should be spoken. You can search the web for "
        "current information; when you do, don't announce that you're searching, and give the answer "
        "directly without reading out sources."
    )


@dataclass
class Conversation:
    messages: list[dict] = field(default_factory=list)
    last_used: float = 0.0

    def add(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})
        limit = config.HISTORY_TURNS * 2
        if len(self.messages) > limit:
            self.messages = self.messages[-limit:]
        # Both APIs want the history to start with a user turn.
        while self.messages and self.messages[0]["role"] != "user":
            self.messages.pop(0)


class Brain:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0))
        self._claude = None
        if config.ANTHROPIC_API_KEY:
            import anthropic

            self._claude = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
        self.local_ready = asyncio.Event()

    @property
    def claude_available(self) -> bool:
        return self._claude is not None

    # ------------------------------------------------------------------ setup
    async def prepare_local_model(self) -> None:
        """Wait for Ollama, pull the model if needed, and load it into memory."""
        while True:
            try:
                r = await self._http.get(f"{config.OLLAMA_URL}/api/tags")
                r.raise_for_status()
                names = {m.get("name") for m in r.json().get("models", [])}
                break
            except Exception as exc:  # Ollama still starting
                log.info("Waiting for Ollama (%s)...", exc.__class__.__name__)
                await asyncio.sleep(3)
        wanted = config.LOCAL_MODEL if ":" in config.LOCAL_MODEL else f"{config.LOCAL_MODEL}:latest"
        if wanted not in names:
            log.info("Pulling %s (first run only, this can take several minutes)...", config.LOCAL_MODEL)
            async with self._http.stream(
                "POST", f"{config.OLLAMA_URL}/api/pull",
                json={"model": config.LOCAL_MODEL}, timeout=None,
            ) as resp:
                last = ""
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    status = json.loads(line).get("status", "")
                    if status != last:
                        log.info("ollama pull: %s", status)
                        last = status
        try:  # warm-up so the first real question is fast
            await self._http.post(
                f"{config.OLLAMA_URL}/api/generate",
                json={"model": config.LOCAL_MODEL, "prompt": "hi", "stream": False,
                      "keep_alive": "24h", "options": {"num_predict": 1}},
            )
        except Exception as exc:
            log.warning("Warm-up failed: %s", exc)
        self.local_ready.set()
        log.info("Local model %s ready", config.LOCAL_MODEL)

    # --------------------------------------------------------------- answering
    async def ask_local(self, history: list[dict], extra: str = "") -> AsyncIterator[str]:
        if not self.local_ready.is_set():
            yield "My local brain is still loading. Please try again in a minute."
            return
        payload = {
            "model": config.LOCAL_MODEL,
            "messages": [{"role": "system", "content": "\n\n".join(p for p in (local_system_prompt(), extra) if p)}] + history,
            "stream": True,
            "keep_alive": "24h",
            "options": {"num_predict": 200, "temperature": 0.6},
        }
        async with self._http.stream("POST", f"{config.OLLAMA_URL}/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                text = chunk.get("message", {}).get("content", "")
                if text:
                    yield text
                if chunk.get("done"):
                    break

    async def ask_claude(self, history: list[dict], extra: str = "",
                         location: dict | None = None,
                         images: list[tuple[str, bytes]] | None = None) -> AsyncIterator[str]:
        """images: [(label, jpeg bytes)] attached to the latest question (camera photos)."""
        kwargs = {}
        if images and history and history[-1]["role"] == "user":
            content: list[dict] = []
            for label, jpeg in images:
                if label:
                    content.append({"type": "text", "text": label})
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg",
                    "data": base64.b64encode(jpeg).decode("ascii")}})
            content.append({"type": "text", "text": history[-1]["content"]})
            history = history[:-1] + [{"role": "user", "content": content}]
        if config.CLAUDE_WEB_SEARCH and not images:
            tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
            if location:
                loc = {"type": "approximate", "city": location.get("name", "")}
                if len(location.get("country", "")) == 2:
                    loc["country"] = location["country"].upper()
                if location.get("timezone"):
                    loc["timezone"] = location["timezone"]
                tool["user_location"] = loc
            kwargs["tools"] = [tool]
        system = "\n\n".join(p for p in (claude_system_prompt(), extra) if p)
        messages = list(history)
        for _ in range(3):  # a long web search can pause the turn; continue it
            async with self._claude.messages.stream(
                model=config.CLAUDE_MODEL,
                max_tokens=config.CLAUDE_MAX_TOKENS,
                system=system,
                messages=messages,
                **kwargs,
            ) as stream:
                async for text in stream.text_stream:
                    yield text
                final = await stream.get_final_message()
            if final.stop_reason != "pause_turn":
                return
            blocks = []
            for b in final.content:
                try:
                    blocks.append(b.to_dict())
                except AttributeError:
                    blocks.append(b.model_dump(exclude_none=True))
            messages = messages + [{"role": "assistant", "content": blocks}]


# ---------------------------------------------------------------------------
# Turning a token stream into speakable sentences
# ---------------------------------------------------------------------------
_MD_RE = [
    (re.compile(r"```.*?```", re.DOTALL), " "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),
    (re.compile(r"https?://\S+"), "a link"),
    (re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE), ""),
    (re.compile(r"^\s*#+\s*", re.MULTILINE), ""),
    (re.compile(r"[*_~]{1,3}"), ""),
    (re.compile(r"[\U0001F000-\U0001FAFF☀-➿️]"), ""),
]


def clean_for_speech(text: str) -> str:
    for pattern, repl in _MD_RE:
        text = pattern.sub(repl, text)
    return re.sub(r"\s+", " ", text).strip()


_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]*\s+|\n+")


async def sentences(tokens: AsyncIterator[str], min_len: int = 12) -> AsyncIterator[str]:
    buf = ""
    async for tok in tokens:
        buf += tok
        while True:
            m = None
            for cand in _SENTENCE_END.finditer(buf):
                if cand.start() >= min_len:
                    m = cand
                    break
            if not m:
                if len(buf) > 220:  # very long sentence: break at a comma
                    cut = buf.rfind(", ", 0, 200)
                    if cut > 40:
                        out, buf = buf[:cut + 1], buf[cut + 2:]
                        out = clean_for_speech(out)
                        if out:
                            yield out
                        continue
                break
            out, buf = buf[:m.start()], buf[m.end():]
            out = clean_for_speech(out)
            if out:
                yield out
    out = clean_for_speech(buf)
    if out:
        yield out
