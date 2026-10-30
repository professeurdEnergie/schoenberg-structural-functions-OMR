"""Four-track MIDI export (one track + one channel per voice) using mido."""
from __future__ import annotations

import io
from typing import Iterable, Optional

import mido

from .model import VOICE_NAMES, VOICES, Score

DEFAULT_PROGRAM = 19  # GM "Church Organ": sustained, easy to hear inner voices
PAN = {"S": 40, "A": 56, "T": 72, "B": 88}  # slight stereo spread helps follow voices


def build_midi(score: Score, voices: Iterable[str] = VOICES, program: int = DEFAULT_PROGRAM,
               velocity: int = 80, ticks_per_beat: int = 480) -> mido.MidiFile:
    """Type-1 file: track 0 = tempo/title, then one named track per voice.

    Each voice keeps its own channel (S=1, A=2, T=3, B=4 in 1-based terms) even
    when only a subset is exported, so solo files stay consistent with the full one.
    Notes without a voice are skipped (validation reports them).
    """
    wanted = [v for v in VOICES if v in set(voices)]
    mid = mido.MidiFile(type=1, ticks_per_beat=ticks_per_beat)

    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("track_name", name=score.title or "Harmony excerpt", time=0))
    conductor.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(score.tempo_bpm), time=0))
    conductor.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(conductor)

    for v in wanted:
        ch = VOICES.index(v)
        track = mido.MidiTrack()
        track.append(mido.MetaMessage("track_name", name=VOICE_NAMES[v], time=0))
        track.append(mido.Message("program_change", channel=ch, program=program, time=0))
        track.append(mido.Message("control_change", channel=ch, control=10, value=PAN[v], time=0))

        events = []  # (tick, order, message); note_off sorts before note_on at the same tick
        for n in score.notes:
            if n.voice != v:
                continue
            on = round(n.onset * ticks_per_beat)
            off = round((n.onset + n.duration) * ticks_per_beat)
            if off <= on or not 0 <= n.pitch.midi <= 127:
                continue
            events.append((on, 1, mido.Message("note_on", channel=ch, note=n.pitch.midi, velocity=velocity)))
            events.append((off, 0, mido.Message("note_off", channel=ch, note=n.pitch.midi, velocity=0)))
        events.sort(key=lambda e: (e[0], e[1]))

        now = 0
        for tick, _, msg in events:
            track.append(msg.copy(time=tick - now))
            now = tick
        track.append(mido.MetaMessage("end_of_track", time=0))
        mid.tracks.append(track)
    return mid


def write_midi(score: Score, path: str, voices: Iterable[str] = VOICES,
               program: int = DEFAULT_PROGRAM) -> None:
    build_midi(score, voices, program).save(path)


def midi_bytes(score: Score, voices: Iterable[str] = VOICES,
               program: int = DEFAULT_PROGRAM) -> bytes:
    buf = io.BytesIO()
    build_midi(score, voices, program).save(file=buf)
    return buf.getvalue()


def write_split(score: Score, stem: str, program: int = DEFAULT_PROGRAM,
                out_dir: Optional[str] = None) -> list[str]:
    """One file per voice (<stem>_S.mid ...) for players that cannot mute tracks."""
    import os
    paths = []
    for v in VOICES:
        path = f"{stem}_{v}.mid" if out_dir is None else os.path.join(out_dir, f"{os.path.basename(stem)}_{v}.mid")
        write_midi(score, path, voices=[v], program=program)
        paths.append(path)
    return paths
