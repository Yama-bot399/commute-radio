#!/usr/bin/env python3
"""通勤ラジオ用のオリジナルBGMを作る（numpy で合成 → ffmpeg で MP3 に変換）

  python tools/make_bgm.py        → bgm/ に morning / lofi / ambient / news の4曲を書き出す

どれもこのスクリプトで一から合成した曲なので、著作権を気にせず使えます。
ループ再生してもつなぎ目が出ないよう、余韻を曲の頭に重ねて作っています。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np

SR = 24000
OUT = Path(__file__).resolve().parent.parent / "bgm"
NOTE = {n: i for i, n in enumerate(["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"])}


def hz(name: str) -> float:
    """'A4' → 440.0"""
    n, octave = name[:-1], int(name[-1])
    return 440.0 * 2 ** ((NOTE[n] + 12 * (octave + 1) - 69) / 12)


def lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    """1次のローパス（音を丸くする）"""
    a = np.exp(-2 * np.pi * cutoff / SR)
    y = np.empty_like(x)
    acc = 0.0
    for i, v in enumerate(x):
        acc = (1 - a) * v + a * acc
        y[i] = acc
    return y


def env(n: int, attack: float, decay: float) -> np.ndarray:
    t = np.arange(n) / SR
    e = np.exp(-t / decay)
    a = max(1, int(attack * SR))
    e[:a] *= np.linspace(0, 1, a)
    return e


def epiano(f: float, dur: float, vel: float = 1.0) -> np.ndarray:
    """エレピ風の音（倍音が早く減衰する）"""
    n = int(dur * SR)
    t = np.arange(n) / SR
    x = (np.sin(2 * np.pi * f * t) + 0.35 * np.sin(2 * np.pi * 2 * f * t) * np.exp(-t / 0.25)
         + 0.12 * np.sin(2 * np.pi * 3.01 * f * t) * np.exp(-t / 0.12))
    x *= 1 + 0.004 * np.sin(2 * np.pi * 5 * t)  # わずかなトレモロ
    return vel * x * env(n, 0.004, 0.9)


def pad(f: float, dur: float) -> np.ndarray:
    """ゆっくり立ち上がるパッド音"""
    n = int(dur * SR)
    t = np.arange(n) / SR
    x = sum(np.sin(2 * np.pi * f * k * (1 + d) * t) / k for k, d in ((1, 0), (1, 0.003), (2, -0.002), (3, 0.001)))
    e = np.minimum(1, t / (dur * 0.35)) * np.minimum(1, (dur - t) / (dur * 0.35))
    return x * e


def bass(f: float, dur: float) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    return (np.sin(2 * np.pi * f * t) + 0.2 * np.sin(4 * np.pi * f * t)) * env(n, 0.005, 0.5)


def kick(vel: float = 1.0) -> np.ndarray:
    n = int(0.35 * SR)
    t = np.arange(n) / SR
    f = 50 + 70 * np.exp(-t / 0.04)
    return vel * np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.12)


def noise_hit(dur: float, decay: float, cutoff: float, high: bool, rng) -> np.ndarray:
    n = int(dur * SR)
    x = rng.standard_normal(n)
    lp = lowpass(x, cutoff)
    x = x - lp if high else lp
    return x * env(n, 0.001, decay)


def place(buf: np.ndarray, sig: np.ndarray, at: float, gain: float = 1.0) -> None:
    i = int(at * SR)
    end = i + len(sig)
    if end <= len(buf):
        buf[i:end] += gain * sig
    else:  # 曲の頭へ回り込ませて、ループのつなぎ目を自然にする
        k = len(buf) - i
        buf[i:] += gain * sig[:k]
        buf[: end - len(buf)] += gain * sig[k:]


def chord_tones(ch: list[str]) -> list[float]:
    return [hz(n) for n in ch]


def song(bpm: float, bars: int, chords: list[list[str]], bass_notes: list[str], style: str, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    beat = 60 / bpm
    bar = 4 * beat
    total = bars * bar
    buf = np.zeros(int(round(total * SR)))
    for b in range(bars):
        t0 = b * bar
        ch = chord_tones(chords[b % len(chords)])
        root = hz(bass_notes[b % len(bass_notes)])
        if style in ("ambient",):
            for f in ch:
                place(buf, pad(f, bar * 1.6), t0, 0.10)
            place(buf, pad(root, bar * 1.6), t0, 0.12)
            # ときどき高い音でキラッと
            for k in range(2):
                f = ch[rng.integers(len(ch))] * 2
                place(buf, epiano(f, 2.5, 0.5), t0 + (k * 2 + rng.integers(2)) * beat, 0.10)
            continue
        if style == "morning":
            for s in range(8):  # 8分音符のアルペジオ
                f = ch[[0, 1, 2, 1, 0, 2, 1, 2][s]] * (2 if s % 4 == 3 else 1)
                place(buf, epiano(f, 1.2, 0.8 if s % 2 == 0 else 0.6), t0 + s * beat / 2, 0.16)
            for f in ch:
                place(buf, pad(f / 2, bar * 1.2), t0, 0.04)
            place(buf, bass(root, beat * 1.8), t0, 0.35)
            place(buf, bass(root, beat * 1.8), t0 + 2 * beat, 0.30)
            for s in range(8):
                place(buf, noise_hit(0.08, 0.02, 6000, True, rng), t0 + s * beat / 2, 0.025 if s % 2 else 0.04)
            place(buf, kick(0.6), t0, 0.5)
            place(buf, kick(0.5), t0 + 2 * beat, 0.5)
        elif style == "lofi":
            swing = beat * 0.08
            for k, at in enumerate((0, 1.5, 2.5)):
                for f in ch:
                    place(buf, epiano(f, 1.6, 0.7), t0 + at * beat + (swing if k else 0), 0.10)
            place(buf, bass(root, beat * 1.5), t0, 0.40)
            place(buf, bass(root * 1.5, beat), t0 + 2.5 * beat + swing, 0.25)
            place(buf, kick(0.8), t0, 0.55)
            place(buf, kick(0.6), t0 + 2.5 * beat + swing, 0.45)
            for at in (1, 3):
                place(buf, noise_hit(0.25, 0.08, 1800, False, rng), t0 + at * beat, 0.14)
            for s in range(8):
                place(buf, noise_hit(0.06, 0.015, 7000, True, rng), t0 + s * beat / 2 + (swing if s % 2 else 0), 0.025)
        elif style == "news":
            for s in range(16):  # 16分音符で刻むシンセ
                f = ch[s % 3] * (2 if s % 8 >= 4 else 1)
                place(buf, epiano(f, 0.35, 1.0 if s % 4 == 0 else 0.55), t0 + s * beat / 4, 0.09)
            for f in ch:
                place(buf, pad(f, bar * 1.2), t0, 0.05)
            for at in range(4):
                place(buf, bass(root, beat * 0.9), t0 + at * beat, 0.14)
                place(buf, kick(0.45), t0 + at * beat, 0.35)
                place(buf, noise_hit(0.05, 0.012, 8000, True, rng), t0 + at * beat + beat / 2, 0.035)
    if style == "lofi":  # レコードのかすかなノイズ
        buf += 0.003 * lowpass(rng.standard_normal(len(buf)), 3000)
    buf = lowpass(buf, 9000 if style != "lofi" else 4500)
    return buf / (np.max(np.abs(buf)) + 1e-9) * 0.8


SONGS = {
    "morning": dict(bpm=100, bars=16, seed=1,
                    chords=[["C4", "E4", "G4"], ["G3", "B3", "D4"], ["A3", "C4", "E4"], ["F3", "A3", "C4"]],
                    bass_notes=["C2", "G2", "A2", "F2"]),
    "lofi": dict(bpm=78, bars=12, seed=2,
                 chords=[["D4", "F4", "A4", "C5"], ["G3", "B3", "D4", "F4"], ["C4", "E4", "G4", "B4"], ["A3", "C4", "E4", "G4"]],
                 bass_notes=["D2", "G2", "C2", "A2"]),
    "ambient": dict(bpm=60, bars=12, seed=3,
                    chords=[["F3", "A3", "C4", "E4"], ["C3", "G3", "C4", "E4"], ["D3", "A3", "D4", "F4"], ["A#2", "F3", "A#3", "D4"]],
                    bass_notes=["F2", "C2", "D2", "A#1"]),
    "news": dict(bpm=112, bars=16, seed=4,
                 chords=[["A3", "C4", "E4"], ["F3", "A3", "C4"], ["C4", "E4", "G4"], ["G3", "B3", "D4"]],
                 bass_notes=["A1", "F1", "C2", "G1"]),
}


def main() -> int:
    OUT.mkdir(exist_ok=True)
    for name, spec in SONGS.items():
        x = song(spec["bpm"], spec["bars"], spec["chords"], spec["bass_notes"], name, spec["seed"])
        pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
        out = OUT / f"{name}.mp3"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "pipe:0",
                        "-af", "loudnorm=I=-18:TP=-2", "-ar", str(SR), "-c:a", "libmp3lame", "-b:a", "64k", str(out)],
                       input=pcm, check=True)
        print(f"{out.name}: {len(x) / SR:.1f}秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
