"""USB webcam: take photos for Claude to describe, and turn pan/tilt cameras.

Voice commands (after "Jarvis"):
  "what do you see?" / "can you see me?" / "what am I holding?"   -> photo + Claude
  "look left" / "look up a bit" / "turn the camera right 45 degrees" -> pan / tilt
  "look around"                                                      -> 3 photos, left to right
  "remember this as the door"  then later  "look at the door"        -> saved positions
  "centre the camera" / "look straight ahead"                        -> back to the middle

Pan/tilt works on UVC cameras that expose pan_relative / tilt_relative
(e.g. Logitech QuickCam Orbit / Sphere). Other webcams still do photos.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass

from . import config
from .store import Store

log = logging.getLogger("camera")

STEPS_PER_DEGREE = 64          # UVC pan/tilt units are 1/64 degree on Logitech cameras

_LEAD = r"^\W*(?:(?:hey|ok|okay|so|please|jarvis|can you|could you|would you|now)[\s,]+)*"
_SEE_TAIL = r"(?:,?\s*(?:and|then)\s+(?P<question>.+?))?"
_AMOUNT = (r"(?P<amount>\s+(?:(?:just\s+)?a\s+(?:little\s+)?(?:bit|little|touch|tad)|slightly|a lot|lots|loads|"
           r"all the way|as far as you can|more|a bit more|a little more|much more|"
           r"(?:by\s+)?(?:about\s+)?\d{1,3}\s*(?:degrees?|°)))?")
_VERB = r"(?:look|turn|pan|tilt|point|move|swivel|rotate|face)"
_CAM = r"(?:\s+(?:the|your)\s+(?:camera|head|eye|eyes))?"

MOVE_RE = re.compile(_LEAD + _VERB + _CAM + r"(?:\s+(?:to(?:wards)?\s+)?(?:the|your))?\s+"
                     r"(?P<dir>left|right|up|upwards|down|downwards)" + _AMOUNT + _SEE_TAIL + r"\W*$", re.I)
CENTRE_RE = re.compile(_LEAD + r"(?:(?:centre|center|recentre|recenter|reset|straighten|home)\s+(?:the\s+|your\s+)?"
                       r"(?:camera|view|head)|look\s+(?:straight\s+ahead|straight|forwards?|ahead|at the middle)|"
                       r"(?:turn|point|move)\s+(?:the\s+)?camera\s+(?:to\s+the\s+)?(?:centre|center|middle|forwards?|straight))"
                       + _SEE_TAIL + r"\W*$", re.I)
SAVE_RE = re.compile(_LEAD + r"(?:remember|save|store|mark|call|name)\s+(?:this|that|the current|where you'?re looking)"
                     r"(?:\s+camera)?(?:\s+(?:position|spot|view|angle|direction|place))?\s+as\s+(?:the\s+|my\s+|our\s+)?"
                     r"(?P<name>[a-z0-9][\w' -]{0,30}?)\W*$", re.I)
GOTO_RE = re.compile(_LEAD + _VERB + _CAM + r"\s+(?:at|to|towards?|over at|over to)\s+(?:the\s+|my\s+|our\s+)?"
                     r"(?P<name>[a-z0-9][\w' -]{0,30}?)" + _SEE_TAIL + r"\W*$", re.I)
LIST_RE = re.compile(r"\b(?:what|which)\s+(?:camera\s+)?(?:positions|spots|views|places)\s+(?:do you know|have you (?:got|saved)|"
                     r"can you look at|are saved)|\blist (?:the |your )?camera (?:positions|spots|views)\b", re.I)
FORGET_RE = re.compile(_LEAD + r"forget\s+(?:the\s+)?(?:camera\s+)?(?:position|spot|view)\s+(?:called\s+)?(?:the\s+|my\s+)?"
                       r"(?P<name>[a-z0-9][\w' -]{0,30}?)\W*$", re.I)
AROUND_RE = re.compile(_LEAD + r"(?:(?:(?:have|take)\s+a\s+)?look\s+around(?:\s+the\s+room)?|"
                       r"(?:scan|check|sweep)\s+the\s+room)" + _SEE_TAIL + r"\W*$", re.I)
VISION_RE = re.compile(
    r"\bwhat (?:do|can|did) you see\b|\bwhat are you (?:looking at|seeing)\b|\bwhat'?s in front of you\b|"
    r"\b(?:take|have) a (?:look|peek)\b|\blook at (?:this|that|me|it|these|those|us|him|her|them)\b|"
    r"\bcan you see\b|\bdo you see\b|\bare you able to see\b|"
    r"\bdescribe (?:what you see|the room|the view|this|that|me|what'?s in front)\b|"
    r"\bwhat am i (?:holding|wearing|doing|showing you|pointing at)\b|\bhow many fingers\b|"
    r"\bwhat (?:colou?r|kind|type|brand|breed|book|plant) (?:is|are) (?:this|that|these|those|my|it)\b|"
    r"\bwhat is this\b|\bwhat'?s this\b|\bread (?:this|that|what it says)\b|"
    r"\bis (?:there )?any(?:one|body) (?:there|here|in the room)\b|\bwho'?s there\b|\bwho is there\b|"
    r"\bhow do i look\b|\bhow does (?:this|it|the room) look\b|"
    r"\b(?:through|with|using|on|from) (?:the|your) camera\b|\buse (?:the|your) camera\b|\bcheck the camera\b|"
    r"\btake a (?:photo|picture|snapshot|pic)\b", re.I)

# "look at me / this / it" are about the picture, not a saved position
_NOT_PLACES = {"me", "this", "that", "it", "these", "those", "us", "him", "her", "them", "here", "my face",
               "camera", "the camera", "screen"}


@dataclass
class Command:
    kind: str                 # see | move | centre | save | goto | forget | list | around
    pan: float = 0.0          # degrees, + = left
    tilt: float = 0.0         # degrees, + = up
    name: str = ""
    question: str = ""        # ask Claude about the picture afterwards ("" = no picture)


def _amount(word: str | None, axis: str) -> float:
    w = (word or "").strip().lower()
    m = re.search(r"(\d{1,3})", w)
    if m:
        return float(m.group(1))
    full = 30.0 if axis == "pan" else 15.0
    if re.search(r"bit|little|touch|tad|slightly", w) and "more" not in w:
        return full / 2
    if re.search(r"a lot|lots|loads|much more|all the way|as far", w):
        return 180.0 if "all the way" in w or "as far" in w else full * 2
    return full


def _name(raw: str) -> str:
    n = re.sub(r"\s+", " ", raw.lower()).strip(" '-")
    return re.sub(r"^(?:the|my|our)\s+", "", n)


def parse_command(text: str) -> Command | None:
    t = text.strip()
    m = MOVE_RE.match(t)
    if m:
        d = m.group("dir").lower()
        q = (m.group("question") or "").strip()
        if d.startswith("left") or d.startswith("right"):
            deg = _amount(m.group("amount"), "pan")
            return Command("move", pan=deg if d.startswith("left") else -deg, question=q)
        deg = _amount(m.group("amount"), "tilt")
        return Command("move", tilt=deg if d.startswith("up") else -deg, question=q)
    m = CENTRE_RE.match(t)
    if m:
        return Command("centre", question=(m.group("question") or "").strip())
    m = SAVE_RE.match(t)
    if m:
        return Command("save", name=_name(m.group("name")))
    m = FORGET_RE.match(t)
    if m:
        return Command("forget", name=_name(m.group("name")))
    if LIST_RE.search(t):
        return Command("list")
    m = AROUND_RE.match(t)
    if m:
        return Command("around", question=t)
    m = GOTO_RE.match(t)
    if m and _name(m.group("name")) not in _NOT_PLACES:
        return Command("goto", name=_name(m.group("name")), question=(m.group("question") or "").strip())
    if VISION_RE.search(t):
        return Command("see", question=t)
    return None


class Camera:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.device = config.CAMERA_DEVICE
        self.lock = asyncio.Lock()
        self.pan = 0.0               # estimated position since the last reset (degrees)
        self.tilt = 0.0
        self.pan_limit = 70.0        # replaced by the camera's own range when we can read it
        self.tilt_limit = 30.0
        self._can_move: bool | None = None

    # ------------------------------------------------------------------ state
    @property
    def present(self) -> bool:
        return config.CAMERA_ENABLED and os.path.exists(self.device)

    async def _run(self, *args: str, timeout: float = 15) -> tuple[int, bytes, str]:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, b"", "timed out"
        return proc.returncode, out, err.decode(errors="replace").strip()

    async def can_move(self) -> bool:
        if self._can_move is None:
            rc, out, err = await self._run("v4l2-ctl", "-d", self.device, "--list-ctrls")
            text = out.decode(errors="replace")
            self._can_move = rc == 0 and "pan_relative" in text and "tilt_relative" in text
            for axis in ("pan", "tilt"):
                m = re.search(rf"{axis}_relative\b.*?max=(\d+)", text)
                if m and int(m.group(1)) > 0:
                    setattr(self, f"{axis}_limit", int(m.group(1)) / STEPS_PER_DEGREE)
            log.info("camera %s: pan/tilt %s (range ±%.0f° / ±%.0f°)", self.device,
                     "available" if self._can_move else "not available", self.pan_limit, self.tilt_limit)
            if rc != 0:
                log.warning("v4l2-ctl failed: %s", err[-200:])
        return self._can_move

    # ----------------------------------------------------------------- photos
    async def snapshot(self) -> bytes | None:
        """One JPEG from the camera, after letting the exposure settle."""
        async with self.lock:
            base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2"]
            tail = ["-i", self.device, "-ss", str(config.CAMERA_WARMUP_S), "-frames:v", "1",
                    "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
            attempts = [base + ["-video_size", config.CAMERA_RESOLUTION] + tail, base + tail]
            for cmd in attempts if config.CAMERA_RESOLUTION else attempts[1:]:
                rc, out, err = await self._run(*cmd, timeout=20)
                if rc == 0 and out[:2] == b"\xff\xd8":
                    try:
                        folder = config.DATA_DIR / "camera"
                        folder.mkdir(parents=True, exist_ok=True)
                        (folder / "latest.jpg").write_bytes(out)
                    except OSError:
                        pass
                    log.info("snapshot: %d KB", len(out) // 1024)
                    return out
                log.warning("snapshot failed (%s): %s", rc, err[-300:])
            return None

    # ------------------------------------------------------------ pan / tilt
    async def move(self, pan: float = 0.0, tilt: float = 0.0) -> tuple[float, float]:
        """Turn by (pan, tilt) degrees, + = left / up. Returns the movement actually made."""
        async with self.lock:
            target_pan = max(-self.pan_limit, min(self.pan_limit, self.pan + pan))
            target_tilt = max(-self.tilt_limit, min(self.tilt_limit, self.tilt + tilt))
            dp, dt_ = target_pan - self.pan, target_tilt - self.tilt
            sign = -1 if config.CAMERA_PAN_INVERT else 1
            if abs(dp) >= 1:
                await self._run("v4l2-ctl", "-d", self.device, "-c",
                                f"pan_relative={sign * round(dp * STEPS_PER_DEGREE)}")
                await asyncio.sleep(0.4 + abs(dp) / 45)
            if abs(dt_) >= 1:
                await self._run("v4l2-ctl", "-d", self.device, "-c",
                                f"tilt_relative={round(dt_ * STEPS_PER_DEGREE)}")
                await asyncio.sleep(0.4 + abs(dt_) / 45)
            self.pan, self.tilt = target_pan, target_tilt
            return dp, dt_

    async def reset(self) -> None:
        async with self.lock:
            await self._run("v4l2-ctl", "-d", self.device, "-c", "pan_reset=1")
            await asyncio.sleep(2.5)
            await self._run("v4l2-ctl", "-d", self.device, "-c", "tilt_reset=1")
            await asyncio.sleep(2.0)
            self.pan = self.tilt = 0.0

    async def goto(self, pan: float, tilt: float) -> None:
        """Reset to the middle first, so saved positions stay accurate."""
        await self.reset()
        await self.move(pan, tilt)

    # --------------------------------------------------------- saved positions
    def spots(self) -> list[dict]:
        return self.store.items("camera_spots")

    def find_spot(self, name: str) -> dict | None:
        name = _name(name)
        spots = self.spots()
        for s in spots:
            if s["name"] == name:
                return s
        for s in spots:  # "front door" finds "door", "the desk" finds "my desk"
            if name in s["name"] or s["name"] in name:
                return s
        return None

    def save_spot(self, name: str) -> None:
        old = {s["id"] for s in self.spots() if s["name"] == name}
        if old:
            self.store.remove("camera_spots", old)
        self.store.add("camera_spots", {"name": name, "pan": round(self.pan, 1),
                                        "tilt": round(self.tilt, 1), "created": time.time()})

    def forget_spot(self, name: str) -> bool:
        spot = self.find_spot(name)
        if spot:
            self.store.remove("camera_spots", {spot["id"]})
        return spot is not None


def vision_prompt(count: int = 1) -> str:
    what = "photo was" if count == 1 else "photos were"
    return (f"You are seeing through a webcam attached to the user's Raspberry Pi; the attached {what} "
            "taken just now. Answer the user's question about what the camera sees. If they just ask what "
            "you see, describe the main things briefly. Keep it to two to four spoken sentences unless "
            "they ask for more. Don't try to recognise who people are from their faces, but you can "
            "describe them. If something is too dark, blurry or out of view to tell, say so.")
