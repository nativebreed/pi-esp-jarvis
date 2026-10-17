"""Voice assistant server for xiaozhi-esp32 devices.

HTTP  POST/GET /xiaozhi/ota/   -> tells the device where the WebSocket is
WS    /xiaozhi/v1/             -> audio in (Opus 16 kHz), audio out (Opus 24 kHz)
HTTP  GET /health              -> status JSON
HTTP  GET /xiaozhi/events      -> long-poll: the device waits here to be woken (alarms, timers)
HTTP  POST /xiaozhi/gesture    -> motion gestures from the device while it is idle
HTTP  GET /lists               -> shopping / to-do lists and memories in a browser
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import uuid

from aiohttp import WSMsgType, web

from . import camera, config, music, screen, sounds, web_ui
from .brain import Brain, Conversation, is_goodbye, match_claude_trigger, sentences
from .clock import Alert, Clock
from .pihelper import PiHelper
from .reply import Reply
from .skills import _YES, Skills
from .store import Store
from .timeparse import parse_duration
from .weather import Weather
from .speech import (FRAME_MS, OpusDecoder, OpusEncoder, SPEAKER_RATE, SpeechToText,
                     TextToSpeech, UtteranceDetector)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s",
)
log = logging.getLogger("server")

STT = SpeechToText()
TTS = TextToSpeech()
BRAIN = Brain()
CONVERSATIONS: dict[str, Conversation] = {}
PLAYERS: dict[str, music.PlayerState] = {}
LIBRARY = music.Library(config.MUSIC_DIR)
FRAME_BYTES = SPEAKER_RATE * FRAME_MS // 1000 * 2  # 60 ms of 16-bit mono

SESSIONS: dict[str, "Session"] = {}          # device id -> open conversation
PENDING: list[Alert] = []                     # alerts waiting to be rung
WAITERS: dict[str, asyncio.Future] = {}       # device id -> parked long-poll


def on_alert(alert: Alert) -> None:
    log.info("ALERT %s: %s", alert.kind, alert.text)
    PENDING.append(alert)
    deliver_alerts()


def deliver_alerts() -> None:
    """Ring on an open session, or wake an idle device so it connects."""
    if not PENDING:
        return
    if SESSIONS:
        max(SESSIONS.values(), key=lambda s: s.last_activity).start_alerts()
        return
    for fut in WAITERS.values():
        if not fut.done():
            fut.set_result("wake")


STORE = Store()
CLOCK = Clock(STORE, on_alert)
WEATHER = Weather()
HELPER = PiHelper()
SKILLS = Skills(STORE, CLOCK, WEATHER, HELPER)
CAMERA = camera.Camera(STORE)

SNOOZE_RE = re.compile(r"\bsnooze\b|\b(?:\d+|five|ten|fifteen|twenty)\s+more\s+minutes\b|\blater\b|\bnot yet\b", re.I)
DISMISS_RE = re.compile(r"^\W*(?:(?:ok(?:ay)?|jarvis)[\s,]+)?(?:stop|ok(?:ay)?|thanks?|thank you|dismiss|cancel|turn (?:it )?off|"
                        r"shut up|enough|i'?m (?:up|awake)|got it|done|alright|all right|fine|silence|quiet)\b", re.I)


# =============================================================================
# OTA endpoint: the device calls this at boot to learn the WebSocket address
# =============================================================================
async def ota(request: web.Request) -> web.Response:
    version = "1.0.0"
    if request.method == "POST":
        try:
            body = json.loads(await request.text())
            version = (body.get("application") or {}).get("version") or version
        except Exception:
            pass
    ws_url = config.PUBLIC_WS_URL or f"ws://{request.host}/xiaozhi/v1/"
    offset = time.localtime().tm_gmtoff // 60
    log.info("OTA check from %s (fw %s) -> %s", request.headers.get("Device-Id", "?"), version, ws_url)
    return web.json_response({
        "server_time": {"timestamp": int(time.time() * 1000), "timezone_offset": offset},
        "firmware": {"version": version, "url": ""},  # same version = never upgrade
        "websocket": {"url": ws_url, "token": config.AUTH_TOKEN or "local", "version": 1},
    })


async def health(_request: web.Request) -> web.Response:
    return web.json_response({
        "ok": True,
        "local_model": config.LOCAL_MODEL,
        "local_model_ready": BRAIN.local_ready.is_set(),
        "claude_configured": BRAIN.claude_available,
        "claude_model": config.CLAUDE_MODEL,
        "camera": CAMERA.present,
    })


# =============================================================================
# One WebSocket connection = one conversation session with a device
# =============================================================================
class Session:
    def __init__(self, ws: web.WebSocketResponse, device_id: str) -> None:
        self.ws = ws
        self.device_id = device_id
        self.session_id = uuid.uuid4().hex[:12]
        self.decoder = OpusDecoder()
        self.encoder = OpusEncoder()
        self.detector = UtteranceDetector()
        self.listening = False
        self.mode = "auto"
        self.turn_task: asyncio.Task | None = None
        self.last_activity = time.monotonic()
        self.next_goes_to_claude = False
        conv = CONVERSATIONS.get(device_id)
        if conv is None or time.monotonic() - conv.last_used > config.HISTORY_RESET_S:
            conv = Conversation()
            CONVERSATIONS[device_id] = conv
        self.conv = conv
        self.player = PLAYERS.setdefault(device_id, music.PlayerState())
        self.playing = False
        self.mcp_pending: dict[int, asyncio.Future] = {}
        self.mcp_next_id = 100
        self.alerting = False                     # ring task running
        self.ringing: Alert | None = None         # alert currently sounding
        self.alert_waiting: Alert | None = None   # interrupted alert awaiting "stop"/"snooze"
        self.expect = None                        # follow-up handler from the last reply
        self.screen_ok: bool | None = None        # does the board's firmware have screen modes?
        self.screen_sig = None                    # timers last shown on the board's countdown
        self.dice_count = 0
        self.dice_until = 0.0

    # ---------------------------------------------------------------- sending
    async def send_json(self, **msg) -> None:
        if not self.ws.closed:
            msg.setdefault("session_id", self.session_id)
            await self.ws.send_str(json.dumps(msg))

    async def speak(self, text: str) -> None:
        """Synthesize text sentence by sentence and stream it at real-time pace."""
        parts = [p for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p]
        if len(parts) > 1:
            for p in parts:
                await self.speak(p)
            return
        await self.send_json(type="tts", state="sentence_start", text=text)
        audio = await asyncio.to_thread(TTS.synthesize, text)
        await self.play_pcm(audio)

    async def play_pcm(self, audio) -> None:
        """Stream 24 kHz int16 audio to the device at real-time pace."""
        frames = list(self.encoder.frames(audio))
        start = time.monotonic()
        lead = 0.4  # keep ~400 ms buffered on the device
        for i, frame in enumerate(frames):
            if self.ws.closed:
                return
            await self.ws.send_bytes(frame)
            ahead = (i + 1) * FRAME_MS / 1000 - (time.monotonic() - start)
            if ahead > lead:
                await asyncio.sleep(ahead - lead)
        # let the tail of this sentence play out before the next one starts counting
        remaining = len(frames) * FRAME_MS / 1000 - (time.monotonic() - start)
        if remaining > lead:
            await asyncio.sleep(remaining - lead)

    # ------------------------------------------------------------- receiving
    async def on_text(self, data: str) -> None:
        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            return
        kind = msg.get("type")
        if kind == "hello":
            await self.send_json(
                type="hello", transport="websocket",
                audio_params={"format": "opus", "sample_rate": SPEAKER_RATE,
                              "channels": 1, "frame_duration": FRAME_MS},
            )
            asyncio.create_task(self.first_screen_sync())
        elif kind == "listen":
            state = msg.get("state")
            if state == "start":
                self.mode = msg.get("mode", "auto")
                if self.alerting:
                    return
                self.start_listening()
                if PENDING:
                    self.start_alerts()
            elif state == "stop":
                # push-to-talk released
                if self.listening and self.detector.audio:
                    self.finish_utterance()
                else:
                    self.listening = False
            # "detect" (wake word) needs no action: listen/start follows it
        elif kind == "abort":
            if self.playing:  # remember where the music was, for resume / volume
                self.player.paused_at = time.monotonic()
            if self.ringing:  # interrupted an alarm: wait for "stop" or "snooze"
                self.alert_waiting, self.ringing = self.ringing, None
            await self.cancel_turn()
            await self.send_json(type="tts", state="stop")
            self.start_listening()  # device usually sends listen/start next anyway
        elif kind == "mcp":
            payload = msg.get("payload") or {}
            if payload.get("method") == "notifications/gesture":
                await self.on_gesture((payload.get("params") or {}).get("type", ""))
                return
            fut = self.mcp_pending.get(payload.get("id"))
            if fut and not fut.done():
                fut.set_result(payload)
        # anything else is ignored

    def start_listening(self) -> None:
        self.detector.reset()
        self.listening = True
        self.last_activity = time.monotonic()

    def on_audio(self, packet: bytes) -> None:
        if not self.listening:
            return
        pcm = self.decoder.decode(packet)
        if not pcm:
            return
        done = self.detector.feed(pcm, auto_stop=self.mode != "manual")
        if self.detector.started:
            self.last_activity = time.monotonic()
        if done:
            self.finish_utterance()

    def finish_utterance(self) -> None:
        audio = self.detector.audio
        self.listening = False
        self.detector.reset()
        self.turn_task = asyncio.create_task(self.run_turn(audio))

    async def cancel_turn(self) -> None:
        task, self.turn_task = self.turn_task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------ a turn
    async def run_turn(self, audio: bytes) -> None:
        try:
            t0 = time.monotonic()
            text = await asyncio.to_thread(STT.transcribe, audio)
            log.info("[%s] heard (%.1fs): %r", self.device_id, time.monotonic() - t0, text)
            if not text:
                self.start_listening()
                return
            await self.send_json(type="stt", text=text)
            self.last_activity = time.monotonic()

            if self.alert_waiting:
                alert, self.alert_waiting = self.alert_waiting, None
                reply = await self.alert_response(alert, text)
                if reply:
                    await self.deliver(reply, text)
                    return

            if self.expect:
                handler, self.expect = self.expect, None
                reply = await handler(text)
                if reply:
                    await self.deliver(reply, text)
                    return

            if is_goodbye(text):
                await self.send_json(type="tts", state="start")
                await self.speak("Goodbye!")
                await self.send_json(type="tts", state="stop")
                await asyncio.sleep(0.5)
                await self.ws.close()
                return

            if CAMERA.present:
                cam = camera.parse_command(text)
                if cam:
                    await self.handle_camera(cam, text)
                    return

            if screen.FLIP_RE.match(text):
                await self.fix_flip()
                return

            recent = self.dice_count if time.monotonic() < self.dice_until + 30 else None
            dice = screen.parse_dice(text, recent)
            if dice:
                await self.roll_dice(dice)
                return
            if time.monotonic() < self.dice_until and screen.DONE_RE.match(text):
                self.dice_until = 0
                await self.send_json(type="tts", state="start")
                await self.screen_cmd("dice_off")
                await self.speak("Okay.")
                await self.send_json(type="tts", state="stop")
                self.start_listening()
                return

            command = music.parse_command(text)
            if command:
                await self.handle_music(command)
                return

            question = match_claude_trigger(text)
            if question is None and not self.next_goes_to_claude:
                reply = await SKILLS.handle(text)
                if reply:
                    await self.deliver(reply, text)
                    return

            use_claude = question is not None or self.next_goes_to_claude
            filler = None
            if question is None:
                question = text
                if (not use_claude and config.AUTO_WEB_SEARCH and config.CLAUDE_WEB_SEARCH
                        and BRAIN.claude_available and SKILLS.wants_live_info(text)):
                    use_claude, filler = True, "Let me look that up."
            self.next_goes_to_claude = False

            await self.send_json(type="llm", emotion="thinking", text="🤔")
            await self.send_json(type="tts", state="start")

            if use_claude and not question:
                self.next_goes_to_claude = True
                await self.speak("Sure, what should I ask Claude?")
            else:
                await self.answer(question, use_claude, filler=filler)
            await self.send_json(type="tts", state="stop")
            self.conv.last_used = time.monotonic()
            self.start_listening()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Turn failed")
            try:
                await self.speak("Sorry, something went wrong.")
                await self.send_json(type="tts", state="stop")
                self.start_listening()
            except Exception:
                pass

    async def answer(self, question: str, use_claude: bool, extra: str = "",
                     filler: str | None = None,
                     images: list[tuple[str, bytes]] | None = None) -> None:
        self.conv.add("user", question)
        extra = "\n\n".join(p for p in (SKILLS.memory_prompt(), extra) if p)
        if use_claude and not BRAIN.claude_available:
            await self.speak("Claude isn't set up yet, so here is my own answer.")
            use_claude = False

        if use_claude:
            log.info("[%s] -> Claude (%s): %s", self.device_id, config.CLAUDE_MODEL, question)
            tokens = BRAIN.ask_claude(list(self.conv.messages), extra=extra,
                                      location=await WEATHER.home(), images=images)
            emotion = "cool"
        else:
            log.info("[%s] -> local (%s): %s", self.device_id, config.LOCAL_MODEL, question)
            tokens = BRAIN.ask_local(list(self.conv.messages), extra=extra)
            emotion = "happy"

        # Generate text and speak it concurrently, sentence by sentence.
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        reply_parts: list[str] = []

        async def produce() -> None:
            try:
                async for sentence in sentences(tokens):
                    reply_parts.append(sentence)
                    await queue.put(sentence)
            except Exception as exc:
                log.error("%s failed: %s", "Claude" if use_claude else "Local model", exc)
                msg = ("Sorry, I couldn't reach Claude right now." if use_claude
                       else "Sorry, my local model didn't respond.")
                await queue.put(msg)
            finally:
                await queue.put(None)

        producer = asyncio.create_task(produce())
        try:
            filler_task = None
            if filler is None:
                filler = config.CLAUDE_FILLER if use_claude else None
            if filler:
                filler_task = asyncio.create_task(self.speak(filler))
            first = True
            while True:
                sentence = await queue.get()
                if sentence is None:
                    break
                if filler_task:
                    await filler_task
                    filler_task = None
                if first:
                    await self.send_json(type="llm", emotion=emotion)
                    first = False
                await self.speak(sentence)
            if filler_task:
                await filler_task
        finally:
            producer.cancel()
        reply = " ".join(reply_parts).strip()
        if reply:
            self.conv.add("assistant", reply)
        else:
            self.conv.messages.pop()  # drop the unanswered question

    # ------------------------------------------------------------- skill replies
    async def deliver(self, reply: Reply, heard: str) -> None:
        """Speak a skill's reply (or let a model answer with the skill's data)."""
        await self.send_json(type="tts", state="start")
        if reply.llm_context and BRAIN.local_ready.is_set():
            await self.send_json(type="llm", emotion="thinking", text="🤔")
            await self.answer(reply.question or heard, use_claude=False, extra=reply.llm_context)
        elif reply.claude and BRAIN.claude_available:
            await self.answer(reply.question or heard, use_claude=True)
        elif reply.text:
            await self.send_json(type="llm", emotion=reply.emotion)
            await self.speak(reply.text)
        if reply.after:
            await reply.after()
        self.expect = reply.expect
        await self.send_json(type="tts", state="stop")
        self.conv.last_used = time.monotonic()
        if reply.end:
            await asyncio.sleep(1.0)
            await self.ws.close()
        else:
            self.start_listening()

    # ------------------------------------------------------------------ alerts
    def start_alerts(self) -> None:
        if not self.alerting:
            self.alerting = True
            asyncio.create_task(self._begin_alerts())

    async def _begin_alerts(self) -> None:
        if self.playing:
            self.player.paused_at = time.monotonic()
        await self.cancel_turn()          # stop whatever was playing or being said
        self.listening = False
        self.turn_task = asyncio.create_task(self.ring_alerts())

    async def ring_alerts(self) -> None:
        try:
            while PENDING and not self.ws.closed:
                alert = PENDING.pop(0)
                if alert.kind in ("timer", "alarm", "announce") and time.time() - alert.created > 900:
                    continue  # too late to be useful
                self.ringing = alert
                try:
                    outcome = await self.ring(alert)
                except asyncio.CancelledError:
                    if self.ws.closed and self.ringing is alert:
                        PENDING.insert(0, alert)  # connection dropped mid-ring: ring it again
                    raise
                self.ringing = None
                if outcome == "timeout" and alert.kind == "alarm":
                    msg = CLOCK.snooze(alert, auto=True)
                    if msg:
                        await self.speak(msg)
        finally:
            self.alerting = False
            self.ringing = None
            if not self.ws.closed:
                await self.send_json(type="tts", state="stop")
                self.start_listening()
                asyncio.create_task(self.after_alerts_screen())

    async def ring(self, alert: Alert) -> str:
        """Sound one alert until it times out (cancelled = interrupted by the user)."""
        log.info("[%s] ringing %s", self.device_id, alert.kind)
        await self.screen_cmd("hide")  # show Visor ringing, not the countdown
        await self.send_json(type="tts", state="start")
        await self.send_json(type="llm", emotion="happy" if alert.kind == "announce" else "alarm")
        if alert.kind in ("announce", "reminder"):
            for round_ in range(1 if alert.kind == "announce" else 2):
                await self.play_pcm(sounds.chime("reminder"))
                await self.speak(alert.text)
                await self.play_pcm(sounds.silence(2.5 if round_ == 0 else 0.3))
            return "done"
        limit = 60 if alert.kind == "alarm" else 30
        start = time.monotonic()
        round_ = 0
        while time.monotonic() - start < limit:
            await self.play_pcm(sounds.chime(alert.kind))
            if round_ % 3 == 0:
                await self.speak(alert.text)
            await self.play_pcm(sounds.silence(0.6))
            round_ += 1
        return "timeout"

    async def alert_response(self, alert: Alert, text: str) -> Reply | None:
        """What the user said right after interrupting an alert."""
        if SNOOZE_RE.search(text):
            minutes = max(1, (parse_duration(text) or 540) // 60)
            if alert.kind == "alarm":
                msg = CLOCK.snooze(alert, minutes)
            elif alert.kind == "timer":
                STORE.add("timers", {"label": alert.item.get("label", ""), "due": time.time() + minutes * 60,
                                     "duration": minutes * 60})
                msg = f"OK, I'll ring again in {minutes} minute{'s' if minutes != 1 else ''}."
            else:
                STORE.add("reminders", {"text": alert.item.get("text", ""), "due": time.time() + minutes * 60})
                msg = f"OK, I'll remind you again in {minutes} minute{'s' if minutes != 1 else ''}."
            return self._then_more_alerts(Reply(msg, emotion="sleepy"))
        if DISMISS_RE.match(text):
            now = time.localtime()
            if (alert.kind == "alarm" and config.BRIEFING_AFTER_ALARM and 4 <= now.tm_hour < 11
                    and not alert.item.get("label")):
                return self._then_more_alerts(Reply(await SKILLS.briefing()))
            return self._then_more_alerts(Reply("Okay."))
        if PENDING:
            self.start_alerts()
        return None  # dismissed; handle what they said normally

    def _then_more_alerts(self, reply: Reply) -> Reply:
        if PENDING:
            async def more() -> None:
                self.start_alerts()
            reply.after = more
        return reply

    async def on_gesture(self, kind: str) -> None:
        log.info("[%s] gesture %s", self.device_id, kind)
        alert = self.ringing or self.alert_waiting
        if alert:
            self.ringing = self.alert_waiting = None
            await self.cancel_turn()
            msg = CLOCK.snooze(alert) if (kind == "face_down" and alert.kind == "alarm") else ""
            reply = Reply(msg, emotion="sleepy", end=not PENDING)
            self.turn_task = asyncio.create_task(self.deliver(self._then_more_alerts(reply), ""))
            return
        if kind in ("face_down", "shake"):  # "be quiet" / "never mind"
            if self.playing:
                self.player.paused_at = time.monotonic()
            await self.cancel_turn()
            await self.send_json(type="tts", state="stop")
            await asyncio.sleep(0.3)
            await self.ws.close()

    # ------------------------------------------------- board screen modes
    async def screen_cmd(self, mode: str, **args) -> bool:
        """Dice / countdown screens drawn by the board (needs the current firmware)."""
        if self.screen_ok is False or self.ws.closed:
            return False
        result = await self.device_tool("self.jarvis.screen", {"mode": mode, **args})
        ok = bool(result) and not result.get("isError")
        if not ok and self.screen_ok is None:
            log.info("[%s] board has no screen modes (older firmware): reflash for dice and countdowns",
                     self.device_id)
        self.screen_ok = ok
        return ok

    @staticmethod
    def timer_signature() -> tuple:
        return tuple(sorted((t["id"], round(t["due"])) for t in STORE.items("timers")))

    async def sync_screen(self, force: bool = False) -> None:
        """Show the soonest timer as a countdown on the board (or clear it)."""
        sig = self.timer_signature()
        if sig == self.screen_sig and not force:
            return
        self.screen_sig = sig
        timers = sorted(STORE.items("timers"), key=lambda t: t["due"])
        if timers:
            t = timers[0]
            caption, icon = screen.timer_caption(t, CLOCK._timer_name(t))
            await self.screen_cmd("timer", seconds=max(0, round(t["due"] - time.time())),
                                  total=int(t.get("duration") or 0), label=caption, icon=icon)
        else:
            await self.screen_cmd("timer_off")

    async def first_screen_sync(self) -> None:
        await asyncio.sleep(0.5)  # let the board finish its handshake
        await self.sync_screen(force=True)

    async def after_alerts_screen(self) -> None:
        await self.screen_cmd("show")
        await self.sync_screen(force=True)

    async def fix_flip(self) -> None:
        """"The screen is upside down": the board turns it round and remembers."""
        await self.send_json(type="tts", state="start")
        result = None
        if self.screen_ok is not False:
            result = await self.device_tool("self.jarvis.screen", {"mode": "flip_fix"})
        if result and not result.get("isError"):
            try:
                log.info("[%s] screen flipped by request: %s", self.device_id, result["content"][0]["text"])
            except (KeyError, IndexError, TypeError):
                pass
            await self.speak("Done. I've turned the screen round, and I'll remember which way is up.")
        else:
            await self.speak("I can't turn the screen with this firmware. Flash the latest version first.")
        await self.send_json(type="tts", state="stop")
        self.start_listening()

    async def roll_dice(self, count: int) -> None:
        a, b = random.randint(1, 6), random.randint(1, 6)
        self.dice_count, self.dice_until = count, time.monotonic() + 30
        log.info("[%s] dice: %s", self.device_id, a if count == 1 else (a, b))
        await self.send_json(type="tts", state="start")
        if await self.screen_cmd("dice", a=a, b=b, count=count):
            await asyncio.sleep(0.9)  # let it tumble before saying the number
        await self.speak(f"You rolled a {a}." if count == 1 else f"{a} and {b}. That's {a + b}.")
        await self.send_json(type="tts", state="stop")
        self.conv.last_used = time.monotonic()
        self.start_listening()

    # ----------------------------------------------------------------- camera
    async def handle_camera(self, cmd: camera.Command, heard: str) -> None:
        log.info("[%s] camera command: %s", self.device_id, cmd)
        await self.send_json(type="tts", state="start")
        await self.camera_turn(cmd, heard)
        await self.send_json(type="tts", state="stop")
        self.conv.last_used = time.monotonic()
        self.start_listening()

    async def camera_turn(self, cmd: camera.Command, heard: str) -> None:
        if cmd.kind in ("move", "centre", "goto", "around", "save") and not await CAMERA.can_move():
            if cmd.kind in ("around", "goto") or cmd.question:
                return await self.camera_look(cmd.question or heard)
            await self.speak("This camera can't turn, but I can still tell you what I see.")
            return

        if cmd.kind == "list":
            names = ["the " + s["name"] for s in CAMERA.spots()]
            await self.speak("I can look at " + (", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1
                             else names[0]) + "." if names else
                             "I don't have any camera positions yet. Point me somewhere and say: remember this as the door.")
            return
        if cmd.kind == "save":
            CAMERA.save_spot(cmd.name)
            await self.send_json(type="llm", emotion="happy")
            await self.speak(f"Got it. Say look at the {cmd.name}, and I'll turn here.")
            return
        if cmd.kind == "forget":
            ok = CAMERA.forget_spot(cmd.name)
            await self.speak(f"OK, I've forgotten the {cmd.name}." if ok else f"I don't have a position called {cmd.name}.")
            return

        await self.send_json(type="llm", emotion="thinking", text="🤔")
        if cmd.kind == "around":
            return await self.camera_look_around(cmd.question or heard)
        if cmd.kind == "goto":
            spot = CAMERA.find_spot(cmd.name)
            if not spot:  # "look at my shirt": not a saved place, just look
                return await self.camera_look(heard)
            say = None if cmd.question else self.speak(f"Looking at the {spot['name']}.")
            await asyncio.gather(CAMERA.goto(spot["pan"], spot["tilt"]), *([say] if say else []))
        elif cmd.kind == "centre":
            say = None if cmd.question else self.speak("Looking straight ahead.")
            await asyncio.gather(CAMERA.reset(), *([say] if say else []))
        elif cmd.kind == "move":
            if cmd.pan:
                word = "left" if cmd.pan > 0 else "right"
            else:
                word = "up" if cmd.tilt > 0 else "down"
            dp, dt_ = await CAMERA.move(cmd.pan, cmd.tilt)
            if abs(dp) < 1 and abs(dt_) < 1:
                await self.speak(f"I can't turn any further {word}.")
                if not cmd.question:
                    return
            elif not cmd.question:
                await self.send_json(type="llm", emotion="happy")
                await self.speak(f"Looking {word}.")
        if cmd.question:
            await self.camera_look(cmd.question, announce=False)

    async def camera_look(self, question: str, announce: bool = True) -> None:
        """Take a photo and let Claude answer the question about it."""
        if not BRAIN.claude_available:
            await self.speak("I need Claude to see. Add your Claude API key to the settings to use the camera.")
            return
        await self.send_json(type="llm", emotion="thinking", text="🤔")
        snap = asyncio.create_task(CAMERA.snapshot())
        if announce:
            await self.speak("Let me take a look.")
        image = await snap
        if not image:
            await self.speak("Sorry, I couldn't get a picture from the camera.")
            return
        await self.answer(question, use_claude=True, extra=camera.vision_prompt(1),
                          filler="", images=[("", image)])

    async def camera_look_around(self, question: str) -> None:
        if not BRAIN.claude_available:
            await self.speak("I need Claude to see. Add your Claude API key to the settings to use the camera.")
            return
        sweep = min(45.0, CAMERA.pan_limit)

        async def take_photos() -> list[tuple[str, bytes]]:
            photos = []
            await CAMERA.goto(sweep, 0)
            for label, step in (("Photo 1, camera turned left:", -sweep), ("Photo 2, straight ahead:", -sweep),
                                ("Photo 3, camera turned right:", sweep)):
                image = await CAMERA.snapshot()
                if image:
                    photos.append((label, image))
                await CAMERA.move(step, 0)   # ends back in the middle
            return photos

        photos, _ = await asyncio.gather(take_photos(), self.speak("Having a look around."))
        if not photos:
            await self.speak("Sorry, I couldn't get a picture from the camera.")
            return
        await self.answer(question, use_claude=True, filler="", images=photos,
                          extra=camera.vision_prompt(len(photos)) + " The photos go from left to right; "
                          "describe the room as one scene rather than photo by photo.")

    # ------------------------------------------------------------------ music
    async def handle_music(self, cmd: music.Command) -> None:
        log.info("[%s] music command: %s", self.device_id, cmd)
        player = self.player
        await self.send_json(type="tts", state="start")

        if cmd.kind == "stop":
            player.paused_at = 0
            await self.speak("Okay.")
            await self.send_json(type="tts", state="stop")
            await asyncio.sleep(0.5)
            await self.ws.close()
            return

        if cmd.kind == "volume":
            level = await self.set_volume(cmd.volume)
            await self.speak(f"Volume {level}." if level is not None else "Sorry, I couldn't change the volume.")
            if player.current and player.paused_at and time.monotonic() - player.paused_at < 120:
                await self.play_current(announce=False)  # carry on with the music
        elif cmd.kind == "resume":
            if player.current:
                await self.play_current(announce=False)
            else:
                await self.speak("There's nothing to resume. Say play, and then a song, artist, genre or station.")
        elif cmd.kind == "next":
            if player.current and player.index + 1 < len(player.tracks):
                player.index += 1
                player.position_s = 0
                await self.play_current(announce=True)
            else:
                await self.speak("That was the last one." if player.current else "Nothing is playing.")
        else:  # play
            await self.send_json(type="llm", emotion="thinking", text="🤔")
            tracks: list[music.Track] = []
            source = "radio"
            if not cmd.radio:
                tracks = await asyncio.to_thread(LIBRARY.search, cmd.query)
                source = "library"
            if not tracks:
                station = cmd.raw if cmd.radio else ""
                station = station.removeprefix("me ").removeprefix("some ").strip()
                tracks = await music.search_radio(cmd.query, station_name=station)
                source = "radio"
            if not tracks:
                what = cmd.query or "music"
                await self.speak(f"Sorry, I couldn't find any {what} to play.")
            else:
                log.info("[%s] playing %d %s tracks for %r", self.device_id, len(tracks), source, cmd.query)
                player.tracks, player.index, player.position_s = tracks, 0, 0.0
                await self.play_current(announce=True)

        await self.send_json(type="tts", state="stop")
        self.start_listening()

    async def play_current(self, announce: bool) -> None:
        """Play from the current track onwards (files) or the current station (radio)."""
        player = self.player
        player.paused_at = 0
        failures = 0
        while player.current:
            track = player.current
            say_it = announce

            async def on_start() -> None:
                # Only announce once the track/station actually produces sound.
                if say_it:
                    await self.send_json(type="llm", emotion="happy")
                    await self.speak(f"Playing {track.title}.")
                else:
                    await self.send_json(type="tts", state="sentence_start", text=track.title)

            ok = await self.stream_track(track, player.position_s, on_start)
            if ok:
                announce = False
            player.position_s = 0
            if not ok:
                failures += 1
                if failures >= 3:
                    await self.speak("Sorry, I couldn't play that.")
                    return
            else:
                failures = 0
                if track.is_stream:  # radio only stops on errors or the time limit
                    return
            if player.index + 1 >= len(player.tracks):
                if not ok:
                    await self.speak("Sorry, I couldn't play that.")
                return
            player.index += 1
            if track.is_stream and not ok:
                log.info("[%s] station failed, trying the next one", self.device_id)

    async def stream_track(self, track: music.Track, start_s: float, on_start=None) -> bool:
        """Decode with ffmpeg and stream to the device in real time. False = no audio."""
        proc = await music.open_decoder(track, start_s)
        errors = asyncio.create_task(proc.stderr.read())
        self.playing = True
        got_audio = False
        sent = 0
        lead = 0.5
        t0 = time.monotonic()
        try:
            while not self.ws.closed:
                try:
                    pcm = await asyncio.wait_for(proc.stdout.readexactly(FRAME_BYTES),
                                                 30 if got_audio else 15)
                except asyncio.IncompleteReadError as exc:
                    if not exc.partial:
                        break
                    pcm = exc.partial + b"\0" * (FRAME_BYTES - len(exc.partial))
                except asyncio.TimeoutError:
                    log.warning("[%s] stream stalled: %s", self.device_id, track.title)
                    break
                if not got_audio:
                    got_audio = True
                    if on_start:
                        await on_start()
                    t0 = time.monotonic()
                await self.ws.send_bytes(self.encoder.encode_pcm(pcm))
                sent += 1
                played = sent * FRAME_MS / 1000
                self.player.position_s = start_s + played
                ahead = played - (time.monotonic() - t0)
                if ahead < -1.0:  # network hiccup: don't burst to catch up
                    t0 = time.monotonic() - played
                elif ahead > lead:
                    await asyncio.sleep(ahead - lead)
                if played > config.MAX_PLAY_MINUTES * 60:
                    break
        finally:
            self.playing = False
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.wait()
            err = (await errors).decode(errors="replace").strip()
            if not got_audio:
                log.warning("[%s] could not play %s: %s", self.device_id, track.source, err[-300:])
        return got_audio

    async def device_tool(self, name: str, arguments: dict | None = None) -> dict | None:
        """Call one of the device's built-in MCP tools (volume, brightness, ...)."""
        self.mcp_next_id += 1
        req_id = self.mcp_next_id
        fut = asyncio.get_running_loop().create_future()
        self.mcp_pending[req_id] = fut
        try:
            await self.send_json(type="mcp", payload={
                "jsonrpc": "2.0", "method": "tools/call", "id": req_id,
                "params": {"name": name, "arguments": arguments or {}}})
            reply = await asyncio.wait_for(fut, 3)
            return reply.get("result")
        except asyncio.TimeoutError:
            log.warning("[%s] device did not answer %s", self.device_id, name)
            return None
        finally:
            self.mcp_pending.pop(req_id, None)

    async def set_volume(self, change: str) -> int | None:
        if change in {"+", "-"}:
            current = 70
            status = await self.device_tool("self.get_device_status")
            try:
                current = int(json.loads(status["content"][0]["text"])["audio_speaker"]["volume"])
            except Exception:
                pass
            level = current + (15 if change == "+" else -15)
        else:
            level = int(change)
        level = max(0, min(100, level))
        result = await self.device_tool("self.audio_speaker.set_volume", {"volume": level})
        if result is None or result.get("isError"):
            return None
        return level

    # --------------------------------------------------------------- watchdog
    async def watchdog(self) -> None:
        """Hang up when nobody talks for a while, so the device goes back to idle."""
        while not self.ws.closed:
            await asyncio.sleep(1)
            if self.alerting:
                continue  # an alarm or timer is ringing (or about to)
            busy = self.turn_task is not None and not self.turn_task.done()
            if not self.alerting and self.screen_ok is not False and self.timer_signature() != self.screen_sig:
                asyncio.create_task(self.sync_screen())
            if self.listening and not busy and not self.detector.started:
                if time.monotonic() - self.last_activity > config.IDLE_TIMEOUT_S:
                    log.info("[%s] idle, closing session", self.device_id)
                    await self.ws.close()
                    return


async def websocket(request: web.Request) -> web.WebSocketResponse:
    if config.AUTH_TOKEN:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if token != config.AUTH_TOKEN:
            raise web.HTTPUnauthorized()
    ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024, heartbeat=30)
    await ws.prepare(request)
    device_id = request.headers.get("Device-Id", request.remote or "unknown")
    session = Session(ws, device_id)
    old = SESSIONS.get(device_id)
    SESSIONS[device_id] = session
    if old and not old.ws.closed:
        await old.ws.close()
    log.info("[%s] connected", device_id)
    dog = asyncio.create_task(session.watchdog())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await session.on_text(msg.data)
            elif msg.type == WSMsgType.BINARY:
                session.on_audio(msg.data)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        dog.cancel()
        if SESSIONS.get(device_id) is session:
            del SESSIONS[device_id]
        await session.cancel_turn()
        log.info("[%s] disconnected", device_id)
        if PENDING:
            deliver_alerts()
    return ws


# =============================================================================
# Wake channel and gestures
# =============================================================================
async def events(request: web.Request) -> web.Response:
    """The idle device parks a request here; we answer 'wake' when an alert is due."""
    device = request.query.get("device", "unknown")
    if PENDING and device not in SESSIONS:
        return web.json_response({"event": "wake"})
    fut = asyncio.get_running_loop().create_future()
    old = WAITERS.get(device)
    if old and not old.done():
        old.set_result("none")
    WAITERS[device] = fut
    try:
        event = await asyncio.wait_for(fut, 25)
    except asyncio.TimeoutError:
        event = "none"
    finally:
        if WAITERS.get(device) is fut:
            del WAITERS[device]
    if event == "wake":
        log.info("[%s] waking device for an alert", device)
    return web.json_response({"event": event})


async def gesture(request: web.Request) -> web.Response:
    try:
        body = json.loads(await request.text())
    except Exception:
        body = {}
    device, kind = body.get("device", "unknown"), body.get("type", "")
    session = SESSIONS.get(device)
    if session:
        await session.on_gesture(kind)
    elif kind == "shake":
        timers = STORE.items("timers")
        if timers:   # shake while idle cancels running timers
            STORE.remove("timers", {t["id"] for t in timers})
            on_alert(Alert("announce", "Cancelled your timer." if len(timers) == 1 else "Cancelled your timers."))
    log.info("[%s] gesture %s (idle=%s)", device, kind, session is None)
    return web.json_response({"ok": True})


# =============================================================================
async def on_startup(app: web.Application) -> None:
    async def warm_speech() -> None:
        try:
            await asyncio.to_thread(STT.load)
            await asyncio.to_thread(TTS.load)
            await asyncio.to_thread(TTS.synthesize, "Ready.")
            log.info("Speech models ready")
        except Exception:
            log.exception("Could not load speech models")

    app["bg"] = [asyncio.create_task(warm_speech()),
                 asyncio.create_task(BRAIN.prepare_local_model()),
                 asyncio.create_task(CLOCK.run()),
                 asyncio.create_task(WEATHER.home())]
    log.info("Claude: %s", f"enabled ({config.CLAUDE_MODEL})" if BRAIN.claude_available
             else "not configured (set ANTHROPIC_API_KEY in .env)")
    if CAMERA.present:
        app["bg"].append(asyncio.create_task(CAMERA.can_move()))
    else:
        log.info("Camera: not connected (see docker-compose.camera.yml)")


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_route("*", "/xiaozhi/ota/", ota)
    app.router.add_route("*", "/xiaozhi/ota", ota)
    app.router.add_get("/xiaozhi/v1/", websocket)
    app.router.add_get("/xiaozhi/v1", websocket)
    app.router.add_get("/health", health)
    app.router.add_get("/xiaozhi/events", events)
    app.router.add_post("/xiaozhi/gesture", gesture)
    web_ui.add_routes(app, STORE)
    app.on_startup.append(on_startup)
    return app


if __name__ == "__main__":
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    web.run_app(make_app(), port=config.PORT, access_log=None)
