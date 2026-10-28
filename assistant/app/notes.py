"""Voice notes: recorded on the board, kept on the Pi, transcribed in the background.

Each note is three files in NOTES_DIR, named after when it was recorded (YYYYMMDDHHMMSS):
    20261028143205.ogg    the audio (Opus in Ogg, exactly as the board sent it: no re-encoding)
    20261028143205.txt    the transcript (local Whisper)
    20261028143205.json   title, length, waveform, tidied-up text (optional Claude)
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import config

log = logging.getLogger("notes")

ID_RE = re.compile(r"^\d{14}$")
BARS = 30


# =============================================================================
# Ogg Opus (RFC 7845), written and read by hand so the board's packets are kept as they are
# =============================================================================
def _crc_table() -> list[int]:
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) if r & 0x80000000 else (r << 1)
        table.append(r & 0xFFFFFFFF)
    return table


_CRC = _crc_table()


def _ogg_crc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC[((crc >> 24) & 0xFF) ^ b]
    return crc


def packet_samples(packet: bytes) -> int:
    """Length of one Opus packet in 48 kHz samples (from its TOC byte)."""
    if not packet:
        return 0
    toc = packet[0]
    cfg = toc >> 3
    if cfg < 12:
        frame = (480, 960, 1920, 2880)[cfg % 4]      # SILK: 10/20/40/60 ms
    elif cfg < 16:
        frame = (480, 960)[cfg % 2]                  # hybrid: 10/20 ms
    else:
        frame = (120, 240, 480, 960)[cfg % 4]        # CELT: 2.5/5/10/20 ms
    code = toc & 3
    if code == 0:
        count = 1
    elif code in (1, 2):
        count = 2
    else:
        count = (packet[1] & 0x3F) if len(packet) > 1 else 0
    return frame * count


class OggOpusWriter:
    """Appends Opus packets to an .ogg file a page (about a second) at a time."""
    PRE_SKIP = 312

    def __init__(self, path: Path, input_rate: int = 16000) -> None:
        self.path = path
        self.f = open(path, "wb")
        self.serial = int(time.time() * 1000) & 0x7FFFFFFF
        self.seq = 0
        self.granule = 0
        self.packets = 0
        self.pending: list[bytes] = []
        head = b"OpusHead" + struct.pack("<BBHIhB", 1, 1, self.PRE_SKIP, input_rate, 0, 0)
        vendor = b"pi-esp-jarvis"
        tags = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)
        self._page([head], 0, bos=True)
        self._page([tags], 0)

    def _page(self, packets: list[bytes], granule: int, bos: bool = False, eos: bool = False) -> None:
        lacing = bytearray()
        for p in packets:
            lacing += b"\xff" * (len(p) // 255) + bytes([len(p) % 255])
        flags = (0x02 if bos else 0) | (0x04 if eos else 0)
        header = (b"OggS" + struct.pack("<BBqIII", 0, flags, granule, self.serial, self.seq, 0)
                  + bytes([len(lacing)]) + bytes(lacing))
        body = b"".join(packets)
        crc = _ogg_crc(header + body)
        self.f.write(header[:22] + struct.pack("<I", crc) + header[26:] + body)
        self.f.flush()
        self.seq += 1

    def _segments(self, packets: list[bytes]) -> int:
        return sum(len(p) // 255 + 1 for p in packets)

    def add(self, packet: bytes) -> None:
        if not packet:
            return
        self.pending.append(packet)
        self.packets += 1
        # keep one packet back, so the last page (marked end-of-stream) is never empty
        if len(self.pending) > 17 or self._segments(self.pending) > 240:
            out, self.pending = self.pending[:-1], self.pending[-1:]
            self.granule += sum(packet_samples(p) for p in out)
            self._page(out, self.granule)

    @property
    def seconds(self) -> float:
        return (self.granule + sum(packet_samples(p) for p in self.pending)) / 48000

    def close(self) -> None:
        if self.f.closed:
            return
        if self.pending:
            self.granule += sum(packet_samples(p) for p in self.pending)
            self._page(self.pending, self.granule, eos=True)
            self.pending = []
        self.f.close()


def read_packets(path: Path) -> list[bytes]:
    """The audio packets of an Ogg Opus file (header packets left out)."""
    data = path.read_bytes()
    packets: list[bytes] = []
    partial = b""
    pos = 0
    while pos + 27 <= len(data):
        if data[pos:pos + 4] != b"OggS":
            nxt = data.find(b"OggS", pos + 1)  # damaged: skip to the next page
            if nxt < 0:
                break
            pos = nxt
            continue
        nseg = data[pos + 26]
        lacing = data[pos + 27:pos + 27 + nseg]
        body = pos + 27 + nseg
        for lace in lacing:
            partial += data[body:body + lace]
            body += lace
            if lace < 255:
                packets.append(partial)
                partial = b""
        pos = body
    return [p for p in packets[2:] if p]  # skip OpusHead and OpusTags


# =============================================================================
# The notes on disk
# =============================================================================
@dataclass
class Note:
    id: str
    created: float = 0.0
    seconds: float = 0.0
    title: str = ""
    tidy: str = ""
    wave: str = ""
    gain: float = 1.0
    status: str = "pending"   # pending | done | empty | failed

    @property
    def when(self) -> time.struct_time:
        return time.strptime(self.id, "%Y%m%d%H%M%S")

    def label(self) -> str:
        """Short date for the board: 28 Oct 14:32."""
        t = self.when
        return f"{t.tm_mday} {time.strftime('%b %H:%M', t)}"

    def spoken_date(self) -> str:
        t = self.when
        today = time.localtime()
        day = (today.tm_year, today.tm_yday)
        if (t.tm_year, t.tm_yday) == day:
            when = "today"
        elif (t.tm_year, t.tm_yday + 1) == day:
            when = "yesterday"
        else:
            when = f"on {time.strftime('%A', t)} the {t.tm_mday}{_ordinal(t.tm_mday)}"
        return f"{when} at {time.strftime('%I:%M %p', t).lstrip('0')}"


def _ordinal(n: int) -> str:
    return "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


class NoteStore:
    def __init__(self, folder: Path | None = None) -> None:
        self.dir = folder or config.NOTES_DIR
        self._lock = threading.RLock()

    def ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, note_id: str, ext: str) -> Path:
        if not ID_RE.match(note_id):
            raise ValueError("bad note id")
        return self.dir / f"{note_id}.{ext}"

    def ids(self) -> list[str]:
        """Oldest first."""
        try:
            return sorted(p.stem for p in self.dir.glob("*.ogg") if ID_RE.match(p.stem))
        except FileNotFoundError:
            return []

    def get(self, note_id: str) -> Note | None:
        try:
            audio = self.path(note_id, "ogg")
        except ValueError:
            return None
        if not audio.exists():
            return None
        note = Note(note_id)
        try:
            meta = json.loads(self.path(note_id, "json").read_text())
            for k, v in meta.items():
                if hasattr(note, k) and k != "id":
                    setattr(note, k, v)
        except (FileNotFoundError, ValueError):
            note.created = audio.stat().st_mtime
        return note

    def all(self) -> list[Note]:
        return [n for n in (self.get(i) for i in self.ids()) if n]

    def transcript(self, note_id: str) -> str:
        try:
            return self.path(note_id, "txt").read_text().strip()
        except (FileNotFoundError, ValueError):
            return ""

    def save_meta(self, note: Note) -> None:
        with self._lock:
            meta = {k: getattr(note, k) for k in ("created", "seconds", "title", "tidy", "wave", "gain", "status")}
            tmp = self.path(note.id, "json.tmp")
            tmp.write_text(json.dumps(meta, indent=2))
            tmp.replace(self.path(note.id, "json"))

    def new_id(self) -> str:
        """Now as YYYYMMDDHHMMSS (a second later if that name is taken)."""
        t = time.time()
        while True:
            note_id = time.strftime("%Y%m%d%H%M%S", time.localtime(t))
            if not self.path(note_id, "ogg").exists():
                return note_id
            t += 1

    def delete(self, note_id: str) -> bool:
        found = False
        for ext in ("ogg", "txt", "json"):
            try:
                self.path(note_id, ext).unlink()
                found = True
            except (FileNotFoundError, ValueError):
                pass
        return found

    def search(self, query: str) -> list[Note]:
        words = [w for w in re.findall(r"\w+", query.lower()) if w]
        out = []
        for note in reversed(self.all()):
            hay = " ".join((note.title, note.tidy, self.transcript(note.id))).lower()
            if all(w in hay for w in words):
                out.append(note)
        return out


# =============================================================================
# Transcripts (local Whisper) and optional tidy-up + title (Claude)
# =============================================================================
def decode_pcm16(path: Path, rate: int = 16000) -> bytes:
    import opuslib
    dec = opuslib.Decoder(rate, 1)
    out = bytearray()
    for p in read_packets(path):
        try:
            out += dec.decode(p, max(1, packet_samples(p) * rate // 48000))
        except Exception:
            continue
    return bytes(out)


def plain_title(text: str, words: int = 6) -> str:
    parts = re.findall(r"[\w'’-]+", text)
    if not parts:
        return ""
    title = " ".join(parts[:words])
    return title[0].upper() + title[1:] + ("…" if len(parts) > words else "")


TIDY_PROMPT = (
    "Below is a raw speech-to-text transcript of a voice note someone recorded for themselves. "
    "Reply with only a JSON object with two keys: \"title\" (3 to 6 words saying what the note is about) and "
    "\"text\" (the note tidied up: punctuation, capitals and short paragraphs; drop filler words, false starts "
    "and repeats; fix words the transcription obviously misheard). Keep their words and meaning; don't add "
    "anything, don't summarise, and keep lists as lists.\n\nTranscript:\n"
)


class Transcriber:
    """One note at a time, in the background, so it never competes with live speech for long."""

    def __init__(self, store: NoteStore, stt, brain) -> None:
        self.store, self.stt, self.brain = store, stt, brain
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.busy: str | None = None
        self.listeners: list = []   # called with a note id when its transcript is ready

    def add(self, note_id: str) -> None:
        self.queue.put_nowait(note_id)

    async def run(self) -> None:
        for note in self.store.all():   # anything left over from before a restart
            if note.status == "pending":
                self.add(note.id)
        while True:
            note_id = await self.queue.get()
            self.busy = note_id
            try:
                await self.transcribe(note_id)
            except Exception:
                log.exception("Transcript failed for %s", note_id)
                note = self.store.get(note_id)
                if note:
                    note.status = "failed"
                    self.store.save_meta(note)
            finally:
                self.busy = None
            for fn in list(self.listeners):
                try:
                    fn(note_id)
                except Exception:
                    pass

    async def transcribe(self, note_id: str) -> None:
        note = self.store.get(note_id)
        if not note:
            return
        t0 = time.monotonic()
        pcm = await asyncio.to_thread(decode_pcm16, self.store.path(note_id, "ogg"))
        text = (await asyncio.to_thread(self.stt.transcribe_note, pcm)).strip() if pcm else ""
        self.store.path(note_id, "txt").write_text(text + "\n" if text else "")
        log.info("Transcribed note %s (%.0fs of audio) in %.1fs: %r", note_id, len(pcm) / 32000,
                 time.monotonic() - t0, text[:80])
        note = self.store.get(note_id) or note   # it may have been deleted meanwhile
        if not self.store.path(note_id, "ogg").exists():
            return
        note.status = "done" if text else "empty"
        note.title = plain_title(text)
        if text and config.NOTES_CLAUDE and self.brain.claude_available:
            title, tidy = await self.tidy(text)
            note.title = title or note.title
            note.tidy = tidy
        self.store.save_meta(note)

    async def tidy(self, text: str) -> tuple[str, str]:
        try:
            msg = await self.brain._claude.messages.create(
                model=config.CLAUDE_MODEL, max_tokens=min(4000, 400 + len(text) // 2),
                messages=[{"role": "user", "content": TIDY_PROMPT + text}])
            reply = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            data = json.loads(reply[reply.index("{"):reply.rindex("}") + 1])
            title = str(data.get("title", "")).strip().strip(".")[:80]
            tidy = str(data.get("text", "")).strip()
            return title, tidy
        except Exception as exc:
            log.warning("Claude tidy-up failed: %s", exc)
            return "", ""


# =============================================================================
# Voice commands
# =============================================================================
_OPEN_RE = re.compile(r"^\W*(?:please\s+)?(?:can you\s+|could you\s+)?(?:go (?:in)?to|open|start|switch to|show(?: me)?)?"
                      r"\s*(?:my\s+|the\s+)?(?:voice[- ]?notes?|voice recorder|recorder|dictaphone)"
                      r"(?: mode| app)?\W*(?:please)?\W*$", re.I)
_TAKE_RE = re.compile(r"^\W*(?:please\s+)?(?:can you\s+|could you\s+|i want to\s+|let me\s+|i'd like to\s+)?"
                      r"(?:take|record|make|start)\s+(?:a\s+|another\s+|a new\s+)?(?:voice[- ]?)?(?:note|memo|recording)"
                      r"\W*(?:please)?\W*$", re.I)
_WHICH = r"(?:my\s+|the\s+)?(?:(last|latest|newest|most recent|previous|first|oldest)\s+)?(?:voice[- ]?)?(?:note|memo|recording)"
_PLAY_RE = re.compile(r"^\W*(?:please\s+)?(?:play|play back|replay|listen to)\s+(?:back\s+)?" + _WHICH + r"\W*$", re.I)
_READ_RE = re.compile(r"^\W*(?:please\s+)?(?:read(?: out| back| me)?|what(?:'s| is| was) in|what did i say in|"
                      r"tell me)\s+" + _WHICH + r"\W*$", re.I)
_DELETE_RE = re.compile(r"^\W*(?:please\s+)?(?:delete|remove|erase|bin|throw away)\s+(?:this|that|the|my)?\s*"
                        r"(?:(last|latest|newest)\s+)?(?:voice[- ]?)?(?:note|memo|recording)\W*$", re.I)
_COUNT_RE = re.compile(r"^\W*how many (?:voice[- ]?)?notes", re.I)
# while the recorder is open
_REC_ON_RE = re.compile(r"^\W*(?:start )?(?:record|recording)(?: again)?\W*$|^\W*(?:new note|another one)\W*$", re.I)
_STOP_RE = re.compile(r"^\W*(?:stop|stop it|that's it|done)\W*$", re.I)
_PAUSE_RE = re.compile(r"^\W*(?:pause|wait|hold on)\W*$", re.I)
_RESUME_RE = re.compile(r"^\W*(?:play|resume|continue|carry on|go on|play it(?: again)?)\W*$", re.I)
_NEXT_RE = re.compile(r"^\W*(?:next|next one|next note|newer|forward)\W*$", re.I)
_PREV_RE = re.compile(r"^\W*(?:previous|previous one|previous note|back|go back|older|last one)\W*$", re.I)
_CLOSE_RE = re.compile(r"^\W*(?:close|exit|leave|quit|finish|done with|stop)\s*(?:the\s+)?(?:voice[- ]?notes?|recorder|notes)?"
                       r"(?: mode)?\W*$|^\W*(?:i'm |i am )?(?:all )?(?:done|finished)\W*$|^\W*(?:close|exit|quit)\b.*$", re.I)


@dataclass
class Command:
    kind: str            # open | take | play | read | delete | count
    which: str = "last"  # last | first


def parse_command(text: str) -> Command | None:
    if _TAKE_RE.match(text):
        return Command("take")
    if _OPEN_RE.match(text):
        return Command("open")
    for kind, regex in (("play", _PLAY_RE), ("read", _READ_RE)):
        m = regex.match(text)
        if m:
            which = (m.group(1) or "last").lower()
            return Command(kind, "first" if which in ("first", "oldest") else
                           "previous" if which == "previous" else "last")
    m = _DELETE_RE.match(text)
    if m:
        return Command("delete")
    if _COUNT_RE.match(text):
        return Command("count")
    return None


def recorder_command(text: str) -> str | None:
    """Short commands understood while the recorder is open."""
    for kind, regex in (("rec", _REC_ON_RE), ("pause", _PAUSE_RE), ("next", _NEXT_RE), ("prev", _PREV_RE),
                        ("play", _RESUME_RE), ("stop", _STOP_RE), ("close", _CLOSE_RE)):
        if regex.match(text):
            return kind
    return None


def duration_words(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s} second{'s' if s != 1 else ''}"
    m, s = divmod(s, 60)
    words = f"{m} minute{'s' if m != 1 else ''}"
    return words + (f" {s} second{'s' if s != 1 else ''}" if s and m < 5 else "")


def level_of(pcm: bytes) -> int:
    """0-100 loudness of a chunk of 16-bit audio (-60 dBFS = 0, -10 dBFS = 100)."""
    if not pcm:
        return 0
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    rms = float(np.sqrt(np.mean(a * a))) if len(a) else 0.0
    if rms < 1:
        return 0
    db = 20 * math.log10(rms / 32768)
    return max(0, min(100, int((db + 60) * 2)))


def waveform(levels: list[int], bars: int = BARS) -> str:
    """Squash per-frame levels into a row of digits 0-9 for the board's strip."""
    if not levels:
        return "0" * bars
    out = []
    for i in range(bars):
        a = i * len(levels) // bars
        b = max(a + 1, (i + 1) * len(levels) // bars)
        out.append(str(min(9, max(levels[a:b]) * 9 // 100)))
    return "".join(out)
