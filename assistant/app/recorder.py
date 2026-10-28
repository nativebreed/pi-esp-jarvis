"""The voice-notes recorder for one connected board.

The board draws the recorder (record, pause, stop, play, older/newer, close) and reports taps;
everything else happens here: recording the microphone into a note, playing notes back,
and the voice commands ("take a note", "play my last note", "read my last note", ...).
"""
from __future__ import annotations

import asyncio
import logging
import time

import numpy as np
import webrtcvad

from . import config, sounds
from .notes import (BARS, Command, Note, NoteStore, OggOpusWriter, Transcriber, duration_words, level_of,
                    packet_samples, read_packets, waveform)
from .reply import Reply
from .speech import FRAME_MS, MIC_RATE, SPEAKER_RATE, OpusDecoder

log = logging.getLogger("notes")

FRAME_BYTES = SPEAKER_RATE * FRAME_MS // 1000 * 2
REC, REC_PAUSED, PLAY, PLAY_PAUSED, READY = "rec", "rec_paused", "play", "play_paused", "ready"


class Recorder:
    def __init__(self, session, store: NoteStore, transcriber: Transcriber) -> None:
        self.s = session
        self.store = store
        self.transcriber = transcriber
        self.active = False               # recorder open on the board
        self.state = READY
        self.screen: bool | None = None   # does the board's firmware have the recorder screen?
        self.selected: str | None = None  # note shown on the board
        self.last_note: str | None = None # the note "that note" means
        self.lock = asyncio.Lock()
        self.last_touch = time.monotonic()
        # recording
        self.writer: OggOpusWriter | None = None
        self.rec_id: str | None = None
        self.levels: list[int] = []
        self.peak = 0
        self.heard_voice = False
        self.last_voice = 0.0
        self.pending_levels: list[int] = []
        self.decoder = OpusDecoder()
        self.vad = webrtcvad.Vad(2)
        self.stopping = False
        # playback
        self.play_task: asyncio.Task | None = None
        self.play_pos = 0                 # packet index to carry on from
        self.play_id: str | None = None

    # ------------------------------------------------------------ helpers
    @property
    def recording(self) -> bool:
        return self.state in (REC, REC_PAUSED)

    @property
    def busy(self) -> bool:
        """Recording or playing: the session must not hang up for being quiet."""
        return self.recording or (self.play_task is not None and not self.play_task.done())

    def note(self) -> Note | None:
        return self.store.get(self.selected) if self.selected else None

    def position(self) -> tuple[int, int]:
        ids = self.store.ids()
        if self.selected in ids:
            return ids.index(self.selected) + 1, len(ids)
        return 0, len(ids)

    async def say(self, text: str) -> None:
        await self.s.send_json(type="tts", state="start")
        await self.s.speak(text)
        await self.s.send_json(type="tts", state="stop")
        self.s.start_listening()

    async def show(self, caption: str | None = None) -> bool:
        """Put the recorder on the board as it stands now."""
        if not self.active or self.screen is False or self.s.ws.closed:
            return False
        note = None if self.recording else self.note()
        if self.state == REC:
            header, seconds, total, levels = "REC  new note", int(self.writer.seconds if self.writer else 0), 0, ""
        elif self.state == REC_PAUSED:
            header, seconds, total, levels = "PAUSED  new note", int(self.writer.seconds if self.writer else 0), 0, ""
        elif note:
            i, n = self.position()
            header, total, levels = f"{note.label()} - {i}/{n}", int(round(note.seconds)), note.wave or "0" * BARS
            seconds = int(self.play_seconds()) if self.state in (PLAY, PLAY_PAUSED) else total
        else:
            header, seconds, total, levels = "No notes yet", 0, 0, "0" * BARS
        if caption is None:
            if self.state == REC:
                caption = "Recording - tap stop when you're done"
            elif self.state == REC_PAUSED:
                caption = "Paused - tap pause to carry on"
            elif note:
                caption = note.title or ("Writing it down..." if note.status == "pending" else "(no words heard)")
            else:
                caption = "Tap the red button to record"
        result = await self.s.device_tool("self.jarvis.notes", {
            "state": self.state, "header": header[:100], "caption": caption[:300],
            "seconds": seconds, "total": total, "levels": levels})
        ok = bool(result) and not result.get("isError")
        if result is None and self.screen:
            return False   # one slow answer: keep going, the next update will catch up
        if self.screen is None and not ok:
            log.info("[%s] board has no recorder screen (older firmware): reflash for voice-notes buttons",
                     self.s.device_id)
        self.screen = ok
        return ok

    # ------------------------------------------------------------ recording
    def feed(self, packet: bytes) -> bool:
        """Microphone audio from the board. True = it went into a note (not a voice command)."""
        if self.state != REC or self.writer is None:
            return False
        self.writer.add(packet)
        pcm = self.decoder.decode(packet)
        if pcm:
            level = level_of(pcm)
            self.levels.append(level)
            self.pending_levels.append(level)
            a = np.frombuffer(pcm, dtype=np.int16)
            if len(a):
                self.peak = max(self.peak, int(np.abs(a.astype(np.int32)).max()))
            sub = MIC_RATE * 30 // 1000 * 2
            try:
                voiced = any(self.vad.is_speech(pcm[i:i + sub], MIC_RATE)
                             for i in range(0, len(pcm) - sub + 1, sub))
            except Exception:
                voiced = level > 30
            if voiced:
                self.last_voice = time.monotonic()
                self.heard_voice = True
            if len(self.pending_levels) >= 4 and self.screen:   # about 4 times a second
                level, self.pending_levels = max(self.pending_levels), []
                asyncio.create_task(self.s.device_tool_nowait("self.jarvis.notes", {"state": "level", "level": level}))
        return True

    async def start_recording(self, intro: str | None = None) -> None:
        await self.halt_playback()
        self.store.ensure()
        self.rec_id = self.store.new_id()
        self.writer = OggOpusWriter(self.store.path(self.rec_id, "ogg"))
        self.levels, self.pending_levels, self.peak = [], [], 0
        self.heard_voice, self.stopping = False, False
        await self.s.send_json(type="tts", state="start")
        if intro:
            await self.s.speak(intro)
        else:
            await self.s.play_pcm(sounds.chime("rec"))
        await self.s.send_json(type="tts", state="stop")
        self.last_voice = time.monotonic()
        self.state = REC
        self.s.start_listening()
        log.info("[%s] recording voice note %s", self.s.device_id, self.rec_id)
        await self.show()

    async def finish_recording(self, say: str | None = "Saved.") -> Note | None:
        writer, note_id = self.writer, self.rec_id
        self.writer, self.rec_id, self.state = None, None, READY
        if writer is None or note_id is None:
            return None
        writer.close()
        seconds = writer.seconds
        if seconds < 1.0 or not self.heard_voice and seconds < 4:
            self.store.delete(note_id)
            log.info("[%s] voice note too short, not kept", self.s.device_id)
            await self.show(caption="Too short - not saved")
            if say:
                await self.say("That was too short, so I didn't keep it.")
            return None
        gain = round(min(6.0, max(1.0, 0.8 * 32767 / self.peak)) if self.peak else 1.0, 2)
        note = Note(note_id, created=time.time() - seconds, seconds=round(seconds, 1), wave=waveform(self.levels),
                    gain=gain, status="pending")
        self.store.save_meta(note)
        self.transcriber.add(note_id)
        self.selected = self.last_note = note_id
        log.info("[%s] voice note %s saved (%.1fs)", self.s.device_id, note_id, seconds)
        await self.show(caption="Saved - writing it down...")
        if say:
            await self.say(say)
        return note

    def tick(self) -> None:
        """Once a second from the session watchdog."""
        now = time.monotonic()
        if self.state == REC and self.writer and not self.stopping:
            limit = config.NOTES_SILENCE_STOP_S if self.screen else (5 if self.heard_voice else 12)
            if now - self.last_voice > limit:
                self.stopping = True
                asyncio.create_task(self.auto_stop("silence"))
            elif self.writer.seconds > config.NOTES_MAX_MINUTES * 60:
                self.stopping = True
                asyncio.create_task(self.auto_stop("max"))
        elif self.active and self.state == READY and now - self.last_touch > config.NOTES_IDLE_CLOSE_S:
            self.last_touch = now
            asyncio.create_task(self.close(say=None))

    async def auto_stop(self, why: str) -> None:
        async with self.lock:
            if self.state != REC:
                return
            log.info("[%s] voice note stopped: %s", self.s.device_id, why)
            if why == "max":
                msg = f"That's {int(config.NOTES_MAX_MINUTES)} minutes, so I've stopped and saved it."
            elif self.screen:
                msg = "It's gone quiet, so I've stopped and saved your note."
            else:
                msg = "Saved."
            await self.finish_recording(say=msg)
            if not self.screen:
                self.active = False

    # ------------------------------------------------------------ playback
    def play_seconds(self) -> float:
        return self.play_pos * FRAME_MS / 1000

    async def halt_playback(self) -> None:
        task, self.play_task = self.play_task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            if not self.s.ws.closed:
                await self.s.send_json(type="tts", state="stop")
            self.s.start_listening()

    async def start_playback(self, note_id: str, start: int = 0) -> None:
        await self.halt_playback()
        self.play_id, self.play_pos = note_id, start
        self.last_note = note_id
        self.state = PLAY
        self.play_task = asyncio.create_task(self._playback(note_id, start))
        await self.show()

    async def _playback(self, note_id: str, start: int) -> None:
        import opuslib
        note = self.store.get(note_id)
        if not note:
            self.state = READY
            return
        packets = await asyncio.to_thread(read_packets, self.store.path(note_id, "ogg"))
        dec = opuslib.Decoder(SPEAKER_RATE, 1)
        gain = float(note.gain or 1.0)
        await self.s.send_json(type="tts", state="start")
        pending = b""
        sent, lead, t0 = 0, 0.5, time.monotonic()
        finished = False
        try:
            for i in range(start, len(packets)):
                if self.s.ws.closed:
                    return
                self.play_pos = i
                p = packets[i]
                try:
                    pcm = dec.decode(p, max(1, packet_samples(p) // 2))
                except Exception:
                    continue
                if gain != 1.0:
                    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * gain
                    pcm = np.clip(a, -32768, 32767).astype(np.int16).tobytes()
                pending += pcm
                while len(pending) >= FRAME_BYTES:
                    frame, pending = pending[:FRAME_BYTES], pending[FRAME_BYTES:]
                    await self.s.ws.send_bytes(self.s.encoder.encode_pcm(frame))
                    sent += 1
                    ahead = sent * FRAME_MS / 1000 - (time.monotonic() - t0)
                    if ahead < -1.0:
                        t0 = time.monotonic() - sent * FRAME_MS / 1000
                    elif ahead > lead:
                        await asyncio.sleep(ahead - lead)
            if pending and not self.s.ws.closed:
                await self.s.ws.send_bytes(self.s.encoder.encode_pcm(pending.ljust(FRAME_BYTES, b"\0")))
                sent += 1
            remaining = sent * FRAME_MS / 1000 - (time.monotonic() - t0)
            if remaining > 0:
                await asyncio.sleep(remaining)
            finished = True
        finally:
            if finished and self.play_task is asyncio.current_task():
                self.play_task = None
                self.play_pos, self.state = 0, READY
                if not self.s.ws.closed:
                    await self.s.send_json(type="tts", state="stop")
                    self.s.start_listening()
                    await self.show()

    def interrupted(self) -> None:
        """An alarm is about to ring, or the wake word cut in: pause whatever the recorder is doing.
        (Called from the message loop, so the screen update runs on its own: its answer arrives on that loop.)"""
        if self.state == REC:
            self.state = REC_PAUSED
        elif self.state == PLAY:
            task, self.play_task = self.play_task, None
            if task and not task.done():
                task.cancel()
            self.state = PLAY_PAUSED
        else:
            return
        if self.active:
            asyncio.create_task(self.show())

    async def disconnected(self) -> None:
        """The board hung up: keep what was recorded."""
        task, self.play_task = self.play_task, None
        if task and not task.done():
            task.cancel()
        if self.writer:
            self.active = False
            await self.finish_recording(say=None)
        self.active = False

    # ------------------------------------------------------------ actions
    async def button(self, kind: str) -> None:
        """A tap on the board's recorder (or the same thing said out loud)."""
        if not self.active:
            return
        async with self.lock:
            self.last_touch = time.monotonic()
            log.info("[%s] recorder: %s (%s)", self.s.device_id, kind, self.state)
            if kind == "rec":
                if self.state == REC_PAUSED:
                    self.state, self.last_voice = REC, time.monotonic()
                    await self.show()
                elif self.state != REC:
                    await self.start_recording()
            elif kind == "pause":
                if self.state == REC:
                    self.state = REC_PAUSED
                    await self.show()
                elif self.state == REC_PAUSED:
                    self.state, self.last_voice = REC, time.monotonic()
                    await self.show()
                elif self.state == PLAY:
                    await self.halt_playback()
                    self.state = PLAY_PAUSED
                    await self.show()
                elif self.state == PLAY_PAUSED and self.play_id:
                    await self.start_playback(self.play_id, self.play_pos)
            elif kind == "stop":
                if self.recording:
                    await self.finish_recording()
                elif self.state in (PLAY, PLAY_PAUSED):
                    await self.halt_playback()
                    self.state, self.play_pos = READY, 0
                    await self.show()
            elif kind == "play":
                if self.state == PLAY_PAUSED and self.play_id == self.selected:
                    await self.start_playback(self.selected, self.play_pos)
                elif self.state in (READY, PLAY_PAUSED):
                    if self.selected and self.note():
                        await self.start_playback(self.selected)
                    else:
                        await self.say("There are no notes yet. Tap the red button to record one.")
            elif kind in ("prev", "next"):
                if self.recording:
                    return
                ids = self.store.ids()
                if not ids:
                    return
                i = ids.index(self.selected) if self.selected in ids else len(ids) - 1
                j = max(0, min(len(ids) - 1, i + (1 if kind == "next" else -1)))
                await self.halt_playback()
                self.selected, self.state, self.play_pos = ids[j], READY, 0
                self.last_note = self.selected
                await self.show(caption=None if j != i else ("That's the newest one" if kind == "next"
                                                            else "That's the first one"))
            elif kind == "close":
                await self.close_locked()

    async def open(self) -> bool:
        """Show the recorder with the newest note selected. False = this firmware can't."""
        ids = self.store.ids()
        if self.selected not in ids:
            self.selected = ids[-1] if ids else None
        self.active, self.state, self.play_pos = True, READY, 0
        self.last_touch = time.monotonic()
        if not await self.show():
            self.active = False
            return False
        return True

    async def close(self, say: str | None = "Voice notes closed.") -> None:
        async with self.lock:
            await self.close_locked(say)

    async def close_locked(self, say: str | None = "Voice notes closed.") -> None:
        saved = None
        if self.recording:
            saved = await self.finish_recording(say=None)
        await self.halt_playback()
        self.state, self.play_pos = READY, 0
        if self.active and not self.s.ws.closed:
            await self.s.device_tool("self.jarvis.notes", {"state": "off"})
        self.active = False
        if say:
            await self.say(("Saved. " if saved else "") + say)

    # ------------------------------------------------------------ voice commands
    async def voice(self, kind: str) -> None:
        """A short command said while the recorder is open."""
        if kind == "stop" and self.state == READY:
            kind = "close"
        if kind == "close":
            await self.close()
            return
        await self.button(kind)
        if kind not in ("rec", "play") or self.state not in (REC, PLAY):
            self.s.start_listening()

    def pick(self, which: str) -> str | None:
        ids = self.store.ids()
        if not ids:
            return None
        if which == "first":
            return ids[0]
        if which == "previous":
            return ids[-2] if len(ids) > 1 else ids[0]
        return ids[-1]

    async def command(self, cmd: Command) -> Reply | None:
        """"Go into voice notes", "take a note", "play/read/delete my last note"."""
        if cmd.kind == "open":
            if self.active:
                await self.show()
                await self.say("The recorder's open. Tap the red button to record.")
            elif await self.open():
                n = len(self.store.ids())
                await self.say("Voice notes. Tap the red button to record." if not n else
                               f"Voice notes. You have {n}. Tap the red button to record, or play to listen.")
            else:
                await self.say("The recorder screen needs the latest firmware on my board. Until then, say: "
                               "take a note, play my last note, or read my last note.")
            return None
        if cmd.kind == "take":
            if self.recording:
                return None
            if self.active or await self.open():
                async with self.lock:
                    await self.start_recording("Recording. Tap stop when you're done.")
            else:   # older firmware: record by voice alone, stop after a few seconds of quiet
                self.active = False
                self.screen = False
                self.state = READY
                await self.start_recording("Go ahead, I'm recording. Stop talking for a few seconds when you're done.")
            return None
        if cmd.kind == "count":
            n = len(self.store.ids())
            return Reply(f"You have {n} voice note{'s' if n != 1 else ''}." if n else "You don't have any voice notes yet.")

        note_id = self.selected if (self.active and cmd.which == "last" and cmd.kind == "delete") else None
        if cmd.kind == "delete" and not note_id:
            note_id = self.last_note if self.last_note and self.store.get(self.last_note) else None
        note_id = note_id or self.pick(cmd.which)
        note = self.store.get(note_id) if note_id else None
        if not note:
            return Reply("You don't have any voice notes yet. Say: take a note, to record one.")

        if cmd.kind == "play":
            if not self.active:
                await self.open()
            self.selected = note.id
            async with self.lock:
                if self.screen:
                    await self.start_playback(note.id)
                else:   # no recorder screen: just play it
                    await self.s.send_json(type="tts", state="start")
                    await self.s.speak(f"Your note from {note.spoken_date()}.")
                    await self.s.send_json(type="tts", state="stop")
                    await self.start_playback(note.id)
            return None

        if cmd.kind == "read":
            self.last_note = note.id
            text = note.tidy or self.store.transcript(note.id)
            if note.status == "pending" and not text:
                return Reply("I'm still writing that one down. Ask me again in a minute, or I can play it.")
            if not text:
                return Reply(f"I couldn't make out any words in your note from {note.spoken_date()}.")
            return Reply(f"Your note from {note.spoken_date()}. {text}")

        if cmd.kind == "delete":
            self.last_note = note.id
            what = f"your note from {note.spoken_date()}" + (f", {note.title}" if note.title else "")

            async def confirm(text: str) -> Reply | None:
                from .skills import _YES
                if not _YES.match(text):
                    return Reply("OK, I'll keep it.")
                async with self.lock:
                    if self.play_id == note.id:
                        await self.halt_playback()
                        self.state, self.play_pos = READY, 0
                    self.store.delete(note.id)
                    log.info("[%s] voice note %s deleted", self.s.device_id, note.id)
                    ids = self.store.ids()
                    if self.selected == note.id or self.selected not in ids:
                        self.selected = ids[-1] if ids else None
                    self.last_note = None
                    await self.show(caption="Deleted")
                return Reply("Deleted.")
            return Reply(f"Delete {what}? It's {duration_words(note.seconds)} long.", emotion="confused",
                         expect=confirm)
        return None
