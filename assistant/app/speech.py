"""Speech-to-text (faster-whisper), text-to-speech (Piper), Opus and VAD helpers."""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import opuslib
import webrtcvad

from . import config

log = logging.getLogger("speech")

MIC_RATE = 16000          # device uplink: Opus 16 kHz mono
SPEAKER_RATE = 24000      # device downlink: board codec runs at 24 kHz
FRAME_MS = 60
MIC_FRAME_SAMPLES = MIC_RATE * FRAME_MS // 1000          # 960
SPEAKER_FRAME_SAMPLES = SPEAKER_RATE * FRAME_MS // 1000  # 1440


# ----------------------------------------------------------------------------
# Speech to text
# ----------------------------------------------------------------------------
class SpeechToText:
    _HALLUCINATIONS = {
        "", "you", "thank you", "thanks for watching", "thank you for watching",
        "bye", "the end", "subtitles by the amara.org community",
    }

    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            from faster_whisper import WhisperModel

            log.info("Loading Whisper model %s (first run downloads it)...", config.WHISPER_MODEL)
            self._model = WhisperModel(
                config.WHISPER_MODEL,
                device="cpu",
                compute_type="int8",
                cpu_threads=4,
                download_root=str(config.DATA_DIR / "whisper"),
            )
            log.info("Whisper ready")

    def transcribe(self, pcm16: bytes) -> str:
        self.load()
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self._model.transcribe(
            audio,
            language=config.WHISPER_LANGUAGE or None,
            beam_size=1,
            condition_on_previous_text=False,
            vad_filter=False,
            hotwords=f"Claude {config.ASSISTANT_NAME}",
        )
        parts = []
        for seg in segments:
            if seg.no_speech_prob > 0.8 and seg.avg_logprob < -1.0:
                continue
            parts.append(seg.text.strip())
        text = " ".join(p for p in parts if p).strip()
        normalized = re.sub(r"[^a-z. ]", "", text.lower()).strip(" .")
        if normalized in self._HALLUCINATIONS and len(audio) < MIC_RATE * 2:
            return ""
        return text


# ----------------------------------------------------------------------------
# Text to speech
# ----------------------------------------------------------------------------
class TextToSpeech:
    def __init__(self) -> None:
        self._voice = None
        self._lock = threading.Lock()

    def load(self) -> None:
        with self._lock:
            if self._voice is not None:
                return
            from piper import PiperVoice
            from piper.config import SynthesisConfig

            voice_dir = config.DATA_DIR / "piper"
            voice_dir.mkdir(parents=True, exist_ok=True)
            model_path = voice_dir / f"{config.PIPER_VOICE}.onnx"
            if not model_path.exists():
                log.info("Downloading Piper voice %s...", config.PIPER_VOICE)
                subprocess.run(
                    [sys.executable, "-m", "piper.download_voices",
                     "--download-dir", str(voice_dir), config.PIPER_VOICE],
                    check=True,
                )
            self._voice = PiperVoice.load(str(model_path))
            speed = config.SPEECH_SPEED if config.SPEECH_SPEED > 0 else 1.0
            self._syn_config = SynthesisConfig(length_scale=1.0 / speed)
            log.info("Piper voice ready (%s Hz)", self._voice.config.sample_rate)

    def synthesize(self, text: str) -> np.ndarray:
        """Return int16 mono audio at SPEAKER_RATE."""
        self.load()
        chunks = [c.audio_int16_array for c in self._voice.synthesize(text, syn_config=self._syn_config)]
        if not chunks:
            return np.zeros(0, dtype=np.int16)
        audio = np.concatenate(chunks)
        return resample(audio, self._voice.config.sample_rate, SPEAKER_RATE)


def resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or len(audio) == 0:
        return audio.astype(np.int16)
    duration = len(audio) / src_rate
    dst_len = int(round(duration * dst_rate))
    src_t = np.linspace(0.0, duration, num=len(audio), endpoint=False)
    dst_t = np.linspace(0.0, duration, num=dst_len, endpoint=False)
    out = np.interp(dst_t, src_t, audio.astype(np.float32))
    return np.clip(out, -32768, 32767).astype(np.int16)


class OpusEncoder:
    """Encodes 24 kHz int16 audio into 60 ms Opus frames for the device speaker."""

    def __init__(self) -> None:
        self._enc = opuslib.Encoder(SPEAKER_RATE, 1, opuslib.APPLICATION_AUDIO)

    def frames(self, audio: np.ndarray):
        n = SPEAKER_FRAME_SAMPLES
        if len(audio) % n:
            audio = np.concatenate([audio, np.zeros(n - len(audio) % n, dtype=np.int16)])
        for i in range(0, len(audio), n):
            yield self._enc.encode(audio[i:i + n].tobytes(), n)

    def encode_pcm(self, pcm: bytes) -> bytes:
        """Encode exactly one 60 ms frame of 24 kHz int16 PCM."""
        return self._enc.encode(pcm, SPEAKER_FRAME_SAMPLES)


class OpusDecoder:
    """Decodes 16 kHz Opus frames from the device microphone."""

    def __init__(self) -> None:
        self._dec = opuslib.Decoder(MIC_RATE, 1)

    def decode(self, packet: bytes) -> bytes:
        try:
            return self._dec.decode(packet, MIC_FRAME_SAMPLES)
        except opuslib.OpusError as exc:
            log.debug("Bad opus frame: %s", exc)
            return b""


# ----------------------------------------------------------------------------
# End-of-speech detection
# ----------------------------------------------------------------------------
class UtteranceDetector:
    """Collects audio and decides when the user has finished speaking."""

    SUB_MS = 30
    SUB_BYTES = MIC_RATE * SUB_MS // 1000 * 2  # 960 bytes
    PREROLL_MS = 300
    START_MS = 150  # this much speech before we consider it a real utterance

    def __init__(self) -> None:
        self._vad = webrtcvad.Vad(max(0, min(3, config.VAD_AGGRESSIVENESS)))
        self.reset()

    def reset(self) -> None:
        self._preroll: list[bytes] = []
        self._audio = bytearray()
        self.started = False
        self._speech_ms = 0
        self._silence_ms = 0
        self._total_ms = 0
        self._pending = b""

    @property
    def audio(self) -> bytes:
        return bytes(self._audio) if self.started else b"".join(self._preroll)

    def feed(self, pcm: bytes, auto_stop: bool = True) -> bool:
        """Feed decoded PCM. Returns True when the utterance is complete."""
        data = self._pending + pcm
        done = False
        while len(data) >= self.SUB_BYTES:
            sub, data = data[:self.SUB_BYTES], data[self.SUB_BYTES:]
            try:
                is_speech = self._vad.is_speech(sub, MIC_RATE)
            except Exception:
                is_speech = False
            if not self.started:
                self._preroll.append(sub)
                if len(self._preroll) > self.PREROLL_MS // self.SUB_MS:
                    self._preroll.pop(0)
                self._speech_ms = self._speech_ms + self.SUB_MS if is_speech else max(0, self._speech_ms - self.SUB_MS)
                if self._speech_ms >= self.START_MS:
                    self.started = True
                    self._audio.extend(b"".join(self._preroll))
                    self._preroll.clear()
                    self._silence_ms = 0
                continue
            self._audio.extend(sub)
            self._total_ms += self.SUB_MS
            self._silence_ms = 0 if is_speech else self._silence_ms + self.SUB_MS
            if auto_stop and self._silence_ms >= config.SILENCE_MS:
                done = True
            if self._total_ms >= config.MAX_UTTERANCE_S * 1000:
                done = True
        self._pending = data
        return done
