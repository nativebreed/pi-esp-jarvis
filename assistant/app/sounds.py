"""Synthesised chimes for alarms, timers and reminders (24 kHz int16)."""
from __future__ import annotations

import functools

import numpy as np

RATE = 24000


def _bell(freq: float, dur: float, vol: float = 0.5) -> np.ndarray:
    t = np.arange(int(RATE * dur)) / RATE
    env = np.exp(-t * 4.0) * np.minimum(1.0, t * 200)
    tone = (np.sin(2 * np.pi * freq * t) + 0.35 * np.sin(2 * np.pi * freq * 2.01 * t)
            + 0.12 * np.sin(2 * np.pi * freq * 3.02 * t))
    return tone * env * vol


def _seq(notes: list[tuple[float, float, float]], tail: float = 0.4) -> np.ndarray:
    total = max(start + dur for _, start, dur in notes) + tail
    out = np.zeros(int(RATE * total))
    for freq, start, dur in notes:
        b = _bell(freq, dur)
        i = int(start * RATE)
        out[i:i + len(b)] += b
    out /= max(1.0, np.abs(out).max() / 0.9)
    return (out * 32767 * 0.8).astype(np.int16)


@functools.lru_cache(maxsize=None)
def chime(kind: str) -> np.ndarray:
    if kind == "alarm":   # bright rising arpeggio, repeated
        return _seq([(659, 0.0, 0.5), (784, 0.15, 0.5), (988, 0.30, 0.6), (1319, 0.45, 0.9),
                     (659, 1.0, 0.5), (784, 1.15, 0.5), (988, 1.30, 0.6), (1319, 1.45, 0.9)])
    if kind == "rec":     # one short ping: recording starts now
        return _seq([(1319, 0.0, 0.25)], tail=0.05)
    if kind == "timer":   # three quick pings
        return _seq([(1047, 0.0, 0.4), (1047, 0.3, 0.4), (1319, 0.6, 0.8)])
    # reminder / announce: gentle two-note
    return _seq([(784, 0.0, 0.7), (1047, 0.25, 1.0)])


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(RATE * seconds), dtype=np.int16)
