"""Music: voice commands, local library search, internet radio, ffmpeg decoding."""
from __future__ import annotations

import asyncio
import logging
import random
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import config

log = logging.getLogger("music")

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma"}
USER_AGENT = "pi-esp-jarvis/1.0"


# =============================================================================
# Voice commands
# =============================================================================
_LEAD = r"^\W*(?:(?:hey|ok|okay|so|please|jarvis|can you|could you|would you|will you)[\s,]+)*"
_TAIL = r"(?:[\s,]+(?:please|thanks|thank you|now))*\W*$"

PLAY_RE = re.compile(_LEAD + r"(?:play|put on|start playing|stream)(?:\s+(?P<q>.+?))?" + _TAIL, re.I)
STOP_RE = re.compile(_LEAD + r"(?:stop|pause|silence|quiet|shut up)(?:\s+(?:the\s+)?(?:music|playing|radio|song|it|that))?" + _TAIL, re.I)
NEXT_RE = re.compile(_LEAD + r"(?:next|skip)(?:\s+(?:the\s+)?(?:song|track|one|station|this))?" + _TAIL, re.I)
RESUME_RE = re.compile(_LEAD + r"(?:resume|continue|unpause|carry on|keep playing)(?:\s+(?:the\s+)?(?:music|playing|song|radio))?" + _TAIL, re.I)
VOLUME_RE = re.compile(
    r"^\W*(?:(?:hey|ok|okay|please|jarvis|can you|could you)[\s,]+)*(?:"
    r"(?:louder|quieter|softer)|"
    r"(?:turn|crank|bring) (?:it|this|that|the (?:volume|music|sound)) (?:up|down)|"
    r"turn (?:up|down)(?: the (?:volume|music|sound))?|"
    r"(?:set |change )?(?:the )?volume (?:up|down|to|at|\d|max|maximum|full|half|lower|higher|zero|ten|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred)|"
    r"(?:a bit |a little )?(?:louder|quieter)|mute|unmute)", re.I)

_FILLER_WORDS = re.compile(
    r"\b(?:me|some|a bit of|a little|the|any|my|music|songs?|tunes|tracks?|playlist|"
    r"radio|station|on|by|please)\b", re.I)
_RADIO_HINT = re.compile(r"\b(?:radio|station|fm|am)\b", re.I)
_NUM_WORDS = {"zero": 0, "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
              "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
              "max": 100, "maximum": 100, "full": 100, "half": 50, "mute": 0}


@dataclass
class Command:
    kind: str            # play | stop | next | resume | volume
    query: str = ""      # play: what to search for (filler words removed)
    raw: str = ""        # play: exactly what followed "play" (for station names)
    radio: bool = False  # play: user said radio/station
    volume: str = ""     # volume: "+", "-" or an absolute number


def parse_command(text: str) -> Command | None:
    t = text.strip()
    if STOP_RE.match(t):
        return Command("stop")
    if NEXT_RE.match(t):
        return Command("next")
    if RESUME_RE.match(t):
        return Command("resume")
    if VOLUME_RE.match(t):
        low = t.lower()
        m = re.search(r"\b(\d{1,3})\b", low)
        if m:
            return Command("volume", volume=str(min(100, int(m.group(1)))))
        for word, val in _NUM_WORDS.items():
            if re.search(rf"\b{word}\b", low):
                return Command("volume", volume=str(val))
        if re.search(r"\bunmute\b", low):
            return Command("volume", volume="60")
        down = re.search(r"\b(?:down|quieter|softer|lower)\b", low)
        return Command("volume", volume="-" if down else "+")
    m = PLAY_RE.match(t)
    if m:
        raw = (m.group("q") or "").strip()
        if re.match(r"^(?:a\s+)?(?:game|games)\b", raw, re.I):
            return None  # "play a game" is a chat request
        query = re.sub(r"\s+", " ", _FILLER_WORDS.sub(" ", raw)).strip(" .,!?")
        if query.lower() in {"something", "anything", "whatever", "it", "that", "again"}:
            query = ""
        return Command("play", query=query, raw=raw.strip(" .,!?"), radio=bool(_RADIO_HINT.search(raw)))
    return None


# =============================================================================
# What we can play
# =============================================================================
@dataclass
class Track:
    source: str          # file path or stream URL
    title: str           # spoken / shown on screen
    is_stream: bool = False


@dataclass
class PlayerState:
    """Per-device playback memory so 'next', 'resume' and volume changes work."""
    tracks: list[Track] = field(default_factory=list)
    index: int = 0
    position_s: float = 0.0   # where we were interrupted (files only)
    paused_at: float = 0.0    # monotonic time of interruption, 0 = not paused

    @property
    def current(self) -> Track | None:
        return self.tracks[self.index] if 0 <= self.index < len(self.tracks) else None


# ----------------------------------------------------------------------------
# Local library (any audio files in the mounted music folder)
# ----------------------------------------------------------------------------
@dataclass
class _Entry:
    path: Path
    title: str
    artist: str
    haystack: str
    sort_key: tuple


class Library:
    RESCAN_S = 300

    def __init__(self, root: Path) -> None:
        self.root = root
        self._entries: list[_Entry] = []
        self._scanned = 0.0

    def _scan(self) -> None:
        if time.monotonic() - self._scanned < self.RESCAN_S and self._scanned:
            return
        entries: list[_Entry] = []
        if self.root.is_dir():
            try:
                import mutagen
            except ImportError:  # pragma: no cover
                mutagen = None
            for path in self.root.rglob("*"):
                if path.suffix.lower() not in AUDIO_EXTS or not path.is_file():
                    continue
                tags = {}
                if mutagen:
                    try:
                        f = mutagen.File(path, easy=True)
                        if f and f.tags:
                            tags = {k: (v[0] if isinstance(v, list) and v else str(v)) for k, v in f.tags.items()}
                    except Exception:
                        pass
                rel = path.relative_to(self.root)
                title = tags.get("title") or path.stem
                artist = tags.get("artist") or tags.get("albumartist") or ""
                track_no = re.match(r"\d+", str(tags.get("tracknumber", "")))
                hay = " ".join([title, artist, tags.get("album", ""), tags.get("genre", ""),
                                str(rel.with_suffix(""))])
                entries.append(_Entry(path, title, artist, _norm(hay),
                                      (str(rel.parent).lower(), int(track_no.group()) if track_no else 0, rel.name.lower())))
        self._entries = entries
        self._scanned = time.monotonic()
        log.info("Music library: %d files in %s", len(entries), self.root)

    def search(self, query: str) -> list[Track]:
        self._scan()
        if not self._entries:
            return []
        if not query:
            picked = random.sample(self._entries, min(50, len(self._entries)))
            return [self._track(e) for e in picked]
        words = _norm(query).split()
        hits = [e for e in self._entries if all(w in e.haystack for w in words)]
        if not hits:
            return []
        exact = [e for e in hits if _norm(e.title) == _norm(query)]
        if exact:  # a specific song: play it, then the rest of the matches
            rest = [e for e in hits if e not in exact]
            random.shuffle(rest)
            hits = exact + rest
        elif len({e.path.parent for e in hits}) == 1:
            hits.sort(key=lambda e: e.sort_key)  # one album: keep album order
        else:
            random.shuffle(hits)
        return [self._track(e) for e in hits[:200]]

    @staticmethod
    def _track(e: _Entry) -> Track:
        title = f"{e.title} by {e.artist}" if e.artist else e.title
        return Track(str(e.path), title)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower().replace("&", " and ")).strip()


# ----------------------------------------------------------------------------
# Internet radio (radio-browser.info, a free community station directory)
# ----------------------------------------------------------------------------
_FALLBACK_SERVERS = ["de1.api.radio-browser.info", "nl1.api.radio-browser.info",
                     "at1.api.radio-browser.info", "fi1.api.radio-browser.info"]


def _radio_servers() -> list[str]:
    names: list[str] = []
    try:
        for info in socket.getaddrinfo("all.api.radio-browser.info", 443, proto=socket.IPPROTO_TCP):
            try:
                name = socket.gethostbyaddr(info[4][0])[0]
                if name.endswith("radio-browser.info") and name not in names:
                    names.append(name)
            except OSError:
                pass
    except OSError:
        pass
    random.shuffle(names)
    return names + [s for s in _FALLBACK_SERVERS if s not in names]


async def search_radio(query: str, station_name: str = "") -> list[Track]:
    """Search by genre tag; if the user said 'radio'/'station', try the station name first."""
    if not query and not station_name:
        query = config.DEFAULT_RADIO_GENRE
    attempts = []
    if station_name:
        attempts.append(("name", station_name))
    if query:
        attempts += [("tag", query.lower()), ("name", query)]
    servers = await asyncio.to_thread(_radio_servers)
    async with httpx.AsyncClient(timeout=8, headers={"User-Agent": USER_AGENT}) as http:
        for server in servers[:4]:
            try:
                for field_name, value in attempts:
                    r = await http.get(f"https://{server}/json/stations/search", params={
                        field_name: value, "order": "clickcount", "reverse": "true",
                        "hidebroken": "true", "limit": 30})
                    r.raise_for_status()
                    tracks = []
                    for st in r.json():
                        url = st.get("url_resolved") or st.get("url")
                        codec = (st.get("codec") or "").upper()
                        if not url or codec not in {"MP3", "AAC", "AAC+", "OGG", "FLAC", ""}:
                            continue
                        name = re.sub(r"\s+", " ", st.get("name", "a radio station")).strip()[:60]
                        tracks.append(Track(url, name, is_stream=True))
                    if tracks:
                        log.info("Radio '%s' (%s via %s): %d stations", value, field_name, server, len(tracks))
                        return tracks[:10]
                return []
            except Exception as exc:
                log.warning("Radio server %s failed: %s", server, exc)
    return []


# =============================================================================
# Decoding anything (file or stream) to 24 kHz mono PCM with ffmpeg
# =============================================================================
async def open_decoder(track: Track, start_s: float = 0.0) -> asyncio.subprocess.Process:
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if track.is_stream:
        args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                 "-user_agent", USER_AGENT]
    elif start_s > 1:
        args += ["-ss", f"{start_s:.1f}"]
    args += ["-i", track.source, "-vn", "-ac", "1", "-ar", "24000",
             "-af", f"volume={config.MUSIC_GAIN}", "-f", "s16le", "pipe:1"]
    return await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
