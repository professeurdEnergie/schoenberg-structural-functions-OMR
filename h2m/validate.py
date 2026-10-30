"""Flag likely recognition / entry errors.  Works on any Score (image-based or not)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .model import VOICE_NAMES, VOICES, Note, Pitch, Score

# Generous four-part ranges (slightly wider than textbook limits so that only
# real slips -- wrong octave, wrong staff, wrong clef -- are reported).
RANGES = {
    "S": (Pitch.parse("C4"), Pitch.parse("A5")),
    "A": (Pitch.parse("G3"), Pitch.parse("D5")),
    "T": (Pitch.parse("C3"), Pitch.parse("A4")),
    "B": (Pitch.parse("E2"), Pitch.parse("D4")),
}
MAX_UPPER_SPACING = 12  # semitones between S-A and A-T
MAX_LEAP = 12           # semitones between consecutive notes of one voice
MIN_CONFIDENCE = 0.5


@dataclass
class Flag:
    level: str                 # "error" (almost certainly wrong) | "warning" (check it)
    code: str
    message: str
    note_ids: list[str] = field(default_factory=list)
    column: Optional[int] = None

    def to_dict(self) -> dict:
        return {"level": self.level, "code": self.code, "message": self.message,
                "note_ids": self.note_ids, "column": self.column}


def is_tertian(pitches: list[Pitch]) -> bool:
    """Letter-name test: can the chord be read as stacked thirds over some root?

    Uses spelling, so augmented sixths (Ab-C-D-F#) pass, while a notehead snapped
    one step too high/low (C-E-F, C-D-G) usually fails.  A 9th requires a 7th.
    """
    letters = {p.diatonic % 7 for p in pitches}
    if len(letters) < 3:
        return True  # dyads, unisons, open fifths: nothing to judge
    for root in letters:
        rel = {(l - root) % 7 for l in letters}
        if 2 in rel and rel <= {0, 1, 2, 4, 6} and (1 not in rel or 6 in rel):
            return True
    return False


def validate(score: Score) -> list[Flag]:
    flags: list[Flag] = []
    cols = score.columns()

    for n in score.notes:
        if n.voice not in VOICES:
            flags.append(Flag("error", "no_voice", f"{n.pitch} is not assigned to a voice", [n.id]))
        if not 0 <= n.pitch.midi <= 127:
            flags.append(Flag("error", "midi_range", f"{n.pitch} is outside the MIDI range", [n.id]))
        if n.confidence < MIN_CONFIDENCE:
            flags.append(Flag("warning", "low_confidence",
                              f"{n.pitch}: click was between a line and a space "
                              f"(confidence {n.confidence:.2f}) - check the staff position", [n.id]))
        if n.voice in RANGES:
            lo, hi = RANGES[n.voice]
            if not lo.midi <= n.pitch.midi <= hi.midi:
                flags.append(Flag("warning", "range",
                                  f"{VOICE_NAMES[n.voice]} {n.pitch} is outside {lo}-{hi}", [n.id]))

    for col in cols:
        c = col.index + 1
        ids = [n.id for n in col.notes]
        k = len(col.notes)
        if k > 4:
            flags.append(Flag("error", "chord_size", f"chord {c} has {k} notes (expected 4)", ids, col.index))
        elif k < 4:
            present = {n.voice for n in col.notes}
            missing = [v for v in VOICES if v not in present]
            held = [v for v in missing if v in col.sounding]
            detail = ", ".join(f"{v} {'held' if v in held else 'silent'}" for v in missing)
            flags.append(Flag("warning", "chord_size",
                              f"chord {c} has {k} notes (expected 4): {detail}", ids, col.index))

        seen: dict[str, Note] = {}
        for n in col.notes:
            if n.voice in seen:
                flags.append(Flag("error", "duplicate_voice",
                                  f"chord {c}: two notes assigned to {VOICE_NAMES[n.voice]}",
                                  [seen[n.voice].id, n.id], col.index))
            elif n.voice:
                seen[n.voice] = n

        snd = col.sounding
        for upper, lower in zip(VOICES, VOICES[1:]):
            if upper in snd and lower in snd and snd[upper].pitch.midi < snd[lower].pitch.midi:
                flags.append(Flag("warning", "crossing",
                                  f"chord {c}: {VOICE_NAMES[lower]} {snd[lower].pitch} is above "
                                  f"{VOICE_NAMES[upper]} {snd[upper].pitch}",
                                  [snd[upper].id, snd[lower].id], col.index))
        for upper, lower in (("S", "A"), ("A", "T")):
            if upper in snd and lower in snd:
                gap = snd[upper].pitch.midi - snd[lower].pitch.midi
                if gap > MAX_UPPER_SPACING:
                    flags.append(Flag("warning", "spacing",
                                      f"chord {c}: {gap} semitones between {upper} and {lower} (> an octave)",
                                      [snd[upper].id, snd[lower].id], col.index))
        pitches = [n.pitch for n in snd.values()]
        if not is_tertian(pitches):
            names = " ".join(str(p) for p in sorted(pitches, key=lambda p: p.midi))
            flags.append(Flag("warning", "not_tertian",
                              f"chord {c} ({names}) is not stacked thirds - notehead one step "
                              f"off, or a non-chord tone?",
                              [n.id for n in snd.values()], col.index))

    for v in VOICES:
        line = sorted((n for n in score.notes if n.voice == v), key=lambda n: n.onset)
        for a, b in zip(line, line[1:]):
            leap = abs(b.pitch.midi - a.pitch.midi)
            if leap > MAX_LEAP:
                flags.append(Flag("warning", "leap",
                                  f"{VOICE_NAMES[v]} leaps {leap} semitones {a.pitch} -> {b.pitch} "
                                  f"(octave or staff slip?)", [a.id, b.id]))
    return flags
