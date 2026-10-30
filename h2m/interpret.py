"""Turn image observations (staff, staff position, printed accidental, x) into
pitch, onset, duration and voice.  Deterministic: re-run after every edit.

Only notes with an image location are touched; notes typed in JSON or imported
from elsewhere keep their own pitch/onset/voice.
"""
from __future__ import annotations

import bisect
from collections import defaultdict
from itertools import combinations
from typing import Optional

from .model import (ACCIDENTALS, CLEF_BOTTOM_LINE, STEP_NAMES, VOICES, ImageLoc, Note,
                    Pitch, Score, key_signature_alters)

# Typical pitch of each voice, used only to place an incomplete *first* chord.
_VOICE_CENTRE = {"S": 74, "A": 67, "T": 62, "B": 50}


def locate(score: Score, x: float, y: float) -> Optional[tuple[int, int, float]]:
    """Snap a click to (staff index, staff step, confidence).

    Confidence is 1.0 when the click is exactly on a line/space and falls to 0.0
    when it is half-way between a line and a space (ambiguous).
    """
    best = None
    for i, st in enumerate(score.staves):
        top, bottom = st.lines_at(x)
        dist = 0.0 if top <= y <= bottom else min(abs(y - top), abs(y - bottom))
        if best is None or dist < best[0]:
            best = (dist, i)
    if best is None:
        return None
    dist, i = best
    st = score.staves[i]
    if dist > 7 * st.spacing_at(x):  # far outside any staff (title, text...)
        return None
    f = st.step_float(x, y)
    step = round(f)
    confidence = max(0.0, 1.0 - 2.0 * abs(f - step))
    return i, step, confidence


def pitch_for_position(score: Score, staff: int, step: int, alter: int) -> Pitch:
    clef = score.staves[staff].clef
    return Pitch.from_diatonic(CLEF_BOTTOM_LINE[clef] + step, alter)


def step_for_pitch(score: Score, staff: int, pitch: Pitch) -> int:
    return pitch.diatonic - CLEF_BOTTOM_LINE[score.staves[staff].clef]


def interpret(score: Score) -> None:
    located = [n for n in score.notes if n.loc is not None and 0 <= n.loc.staff < len(score.staves)]
    if not located:
        return
    _spell(score, located)
    columns = _group_columns(score, located)
    for i, col in enumerate(columns):
        for n in col:
            n.onset = i * score.chord_duration
            n.duration = score.chord_duration
    _assign_voices(score, columns)
    if score.hold_missing_voices:
        _hold_missing(score, columns)


def _system_notes(score: Score, located: list[Note]) -> dict[int, list[Note]]:
    by_system: dict[int, list[Note]] = defaultdict(list)
    for n in located:
        by_system[score.staves[n.loc.staff].system].append(n)
    for notes in by_system.values():
        notes.sort(key=lambda n: n.loc.x)
    return dict(sorted(by_system.items()))


def _spell(score: Score, located: list[Note]) -> None:
    """Pitch from clef + staff position + key signature + accidentals.

    Standard engraving rule: a printed accidental holds for later notes on the same
    staff position (same staff, same octave) until the next barline.
    """
    key = key_signature_alters(score.key_fifths)
    for system, notes in _system_notes(score, located).items():
        bars = sorted(b.x for b in score.barlines if b.system == system)
        carry: dict[tuple[int, int], int] = {}
        measure = None
        for n in notes:
            m = bisect.bisect_right(bars, n.loc.x)
            if m != measure:
                carry, measure = {}, m
            staff = score.staves[n.loc.staff]
            diatonic = CLEF_BOTTOM_LINE[staff.clef] + n.loc.step
            k = (n.loc.staff, n.loc.step)
            if n.accidental is not None:
                alter = ACCIDENTALS[n.accidental]
                carry[k] = alter
            else:
                alter = carry.get(k, key.get(STEP_NAMES[diatonic % 7], 0))
            n.pitch = Pitch.from_diatonic(diatonic, alter)
            n.loc = ImageLoc(n.loc.x, staff.y_of_step(n.loc.x, n.loc.step), n.loc.staff, n.loc.step)


def _group_columns(score: Score, located: list[Note]) -> list[list[Note]]:
    """Cluster noteheads of all staves in a system by x (single linkage).

    Seconds inside a chord displace a notehead by ~1.2 staff spaces, consecutive
    chords are usually >= 3 spaces apart, hence the default 2.0-space tolerance.
    """
    columns: list[list[Note]] = []
    for system, notes in _system_notes(score, located).items():
        staves = [s for s in score.staves if s.system == system]
        spacing = sorted(s.spacing for s in staves)[len(staves) // 2]
        tol = score.group_tolerance * spacing
        current: list[Note] = []
        last_x = None
        for n in notes:
            if current and n.loc.x - last_x > tol:
                columns.append(current)
                current = []
            current.append(n)
            last_x = n.loc.x
        if current:
            columns.append(current)
    return columns


def _sort_key(n: Note):
    return (n.pitch.midi, n.pitch.diatonic)


def _assign_voices(score: Score, columns: list[list[Note]]) -> None:
    """SATB by pitch order; incomplete chords follow voice-leading continuity.

    Pass 1 - complete chords (as many free notes as free voices): highest = S ...
    lowest = B, i.e. the closed-score rule.  More than 4 notes: the top ones get
    S/A/T, the lowest gets B, the rest stay unassigned (validation reports them).
    Pass 2 - incomplete chords: pick the order-preserving subset of voices that
    minimises the distance to each voice's nearest pitch in the neighbouring
    chords, plus a penalty for putting a bass-staff note in an upper voice (or
    vice versa).  So a 3-note chord keeps its bass as B and the gap where the
    missing voice belongs.  Locked notes keep their voice throughout.
    """
    def split(col):
        locked = [n for n in col if n.voice_locked and n.voice in VOICES]
        ids = {n.id for n in locked}
        taken = {n.voice for n in locked}
        free = sorted((n for n in col if n.id not in ids), key=_sort_key, reverse=True)
        for n in free:
            n.voice, n.voice_locked = None, False
        return free, [v for v in VOICES if v not in taken]

    pending = []
    for i, col in enumerate(columns):
        free, voices = split(col)
        if not free or not voices:
            continue
        if len(free) == len(voices):
            pairs = zip(free, voices)
        elif len(free) > len(voices):
            pairs = list(zip(free[:len(voices) - 1], voices[:-1])) + [(free[-1], voices[-1])]
        else:
            pending.append((i, free, voices))
            continue
        for n, v in pairs:
            n.voice = v

    def reference(i: int, v: str) -> int:
        for j in sorted(range(len(columns)), key=lambda j: (abs(j - i), j > i)):
            if j != i:
                for n in columns[j]:
                    if n.voice == v:
                        return n.pitch.midi
        return _VOICE_CENTRE[v]

    def staff_penalty(n: Note, v: str) -> int:
        clef = score.staves[n.loc.staff].clef if n.loc else None
        if clef == "bass":
            return 0 if v == "B" else 12
        if clef == "treble":
            return 12 if v == "B" else 0
        return 0

    for i, free, voices in pending:  # left to right, so earlier fixes inform later ones
        refs = {v: reference(i, v) for v in voices}

        def cost(combo):
            return sum(abs(n.pitch.midi - refs[v]) + staff_penalty(n, v) for n, v in zip(free, combo))

        for n, v in zip(free, min(combinations(voices, len(free)), key=cost)):
            n.voice = v


def _hold_missing(score: Score, columns: list[list[Note]]) -> None:
    """A voice absent from a printed chord sustains its previous note."""
    end = len(columns) * score.chord_duration
    attacks: dict[str, list[float]] = defaultdict(list)
    for col in columns:
        for n in col:
            if n.voice in VOICES and (not attacks[n.voice] or attacks[n.voice][-1] != n.onset):
                attacks[n.voice].append(n.onset)
    for col in columns:
        for n in col:
            if n.voice in VOICES:
                times = attacks[n.voice]
                i = bisect.bisect_right(times, n.onset)
                n.duration = (times[i] if i < len(times) else end) - n.onset
