"""Symbolic score: the central data structure.

Every note carries pitch, onset, voice, confidence and (optionally) the image
location it was read from.  Everything else -- the editor overlay, validation,
MIDI export -- reads from / writes to this model.
"""
from __future__ import annotations

import bisect
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional

VOICES = ("S", "A", "T", "B")
VOICE_NAMES = {"S": "Soprano", "A": "Alto", "T": "Tenor", "B": "Bass"}

STEP_NAMES = "CDEFGAB"
STEP_SEMITONES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

# Printed accidental glyphs -> alteration in semitones.
ACCIDENTALS = {"bb": -2, "b": -1, "n": 0, "#": 1, "x": 2}
_ALTER_TEXT = {-2: "bb", -1: "b", 0: "", 1: "#", 2: "x"}

# Diatonic index (7 * octave + step) of the bottom staff line for each clef.
CLEF_BOTTOM_LINE = {
    "treble": 7 * 4 + 2,  # E4
    "bass": 7 * 2 + 4,    # G2
    "alto": 7 * 3 + 3,    # F3
    "tenor": 7 * 3 + 1,   # D3
}

_SHARP_ORDER = "FCGDAEB"
_FLAT_ORDER = "BEADGCF"


def key_signature_alters(fifths: int) -> dict[str, int]:
    """Map step letter -> alteration implied by a key signature (-7..7)."""
    if fifths >= 0:
        return {s: 1 for s in _SHARP_ORDER[:fifths]}
    return {s: -1 for s in _FLAT_ORDER[:-fifths]}


@dataclass(frozen=True)
class Pitch:
    """Spelled pitch.  Spelling is kept (Bbb4 != A4) even though MIDI merges them."""

    step: str    # C D E F G A B
    alter: int   # -2 .. +2
    octave: int  # scientific pitch notation, C4 = middle C

    @property
    def midi(self) -> int:
        return 12 * (self.octave + 1) + STEP_SEMITONES[self.step] + self.alter

    @property
    def diatonic(self) -> int:
        return 7 * self.octave + STEP_NAMES.index(self.step)

    @classmethod
    def from_diatonic(cls, diatonic: int, alter: int = 0) -> "Pitch":
        return cls(STEP_NAMES[diatonic % 7], alter, diatonic // 7)

    @classmethod
    def parse(cls, text: str) -> "Pitch":
        """Parse 'C4', 'Bb3', 'Bbb4', 'F#4', 'F##4', 'Fx4', 'Bn3'."""
        m = re.fullmatch(r"\s*([A-Ga-g])(bb|b|##|#|x|n)?(-?\d+)\s*", text)
        if not m:
            raise ValueError(f"not a pitch: {text!r} (expected e.g. C4, Bb3, Bbb4, F#4)")
        acc = {"##": "x"}.get(m.group(2) or "", m.group(2) or "")
        return cls(m.group(1).upper(), ACCIDENTALS.get(acc, 0), int(m.group(3)))

    def __str__(self) -> str:
        return f"{self.step}{_ALTER_TEXT[self.alter]}{self.octave}"


@dataclass
class ImageLoc:
    """Where a notehead sits on the source image."""

    x: float    # notehead centre, image pixels
    y: float    # snapped to the staff position below
    staff: int  # index into Score.staves
    step: int   # staff position: 0 = bottom line, 1 = first space, ... 8 = top line;
                # <0 / >8 are ledger positions

    def to_dict(self) -> dict:
        return {"x": round(self.x, 2), "y": round(self.y, 2), "staff": self.staff, "step": self.step}

    @classmethod
    def from_dict(cls, d: dict) -> "ImageLoc":
        return cls(float(d["x"]), float(d["y"]), int(d["staff"]), int(d["step"]))


@dataclass
class Note:
    pitch: Pitch
    onset: float = 0.0               # quarter-note beats from the start
    duration: float = 2.0            # quarter-note beats
    voice: Optional[str] = None      # "S" | "A" | "T" | "B"
    confidence: float = 1.0          # 0..1; 1.0 = placed/verified by a human
    loc: Optional[ImageLoc] = None   # None for notes not read from an image
    accidental: Optional[str] = None  # printed glyph next to the notehead: bb b n # x
    voice_locked: bool = False       # user override; auto-assignment leaves it alone
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pitch": str(self.pitch),
            "midi": self.pitch.midi,  # derived, for readability; ignored on load
            "onset": round(self.onset, 4),
            "duration": round(self.duration, 4),
            "voice": self.voice,
            "confidence": round(self.confidence, 3),
            "accidental": self.accidental,
            "voice_locked": self.voice_locked,
            "loc": self.loc.to_dict() if self.loc else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Note":
        acc = d.get("accidental")
        if acc is not None and acc not in ACCIDENTALS:
            raise ValueError(f"note {d.get('id')}: unknown accidental {acc!r}")
        return cls(
            id=str(d.get("id") or uuid.uuid4().hex[:8]),
            pitch=Pitch.parse(d["pitch"]),
            onset=float(d.get("onset", 0.0)),
            duration=float(d.get("duration", 2.0)),
            voice=d.get("voice"),
            confidence=float(d.get("confidence", 1.0)),
            accidental=acc,
            voice_locked=bool(d.get("voice_locked", False)),
            loc=ImageLoc.from_dict(d["loc"]) if d.get("loc") else None,
        )


@dataclass
class Staff:
    """Staff geometry measured on the image.

    `samples` holds (x, y_top_line, y_bottom_line) at several x positions, so
    slightly rotated or curved staves (photos of a bound book) are handled by
    piecewise-linear interpolation.  The 5 lines are assumed equally spaced locally.
    """

    clef: str
    system: int
    samples: list[tuple[float, float, float]]

    def lines_at(self, x: float) -> tuple[float, float]:
        s = self.samples
        if len(s) == 1:
            return s[0][1], s[0][2]
        xs = [p[0] for p in s]
        # interpolate inside, extrapolate linearly from the end segments outside
        i = min(max(bisect.bisect_left(xs, x), 1), len(s) - 1)
        (x0, t0, b0), (x1, t1, b1) = s[i - 1], s[i]
        f = (x - x0) / (x1 - x0) if x1 != x0 else 0.0
        return t0 + f * (t1 - t0), b0 + f * (b1 - b0)

    def spacing_at(self, x: float) -> float:
        top, bottom = self.lines_at(x)
        return (bottom - top) / 4.0

    def y_of_step(self, x: float, step: int) -> float:
        _, bottom = self.lines_at(x)
        return bottom - step * self.spacing_at(x) / 2.0

    def step_float(self, x: float, y: float) -> float:
        _, bottom = self.lines_at(x)
        return (bottom - y) / (self.spacing_at(x) / 2.0)

    @property
    def x_range(self) -> tuple[float, float]:
        return self.samples[0][0], self.samples[-1][0]

    @property
    def spacing(self) -> float:
        vals = sorted((b - t) / 4.0 for _, t, b in self.samples)
        return vals[len(vals) // 2]

    def to_dict(self) -> dict:
        return {"clef": self.clef, "system": self.system,
                "samples": [[round(a, 2), round(b, 2), round(c, 2)] for a, b, c in self.samples]}

    @classmethod
    def from_dict(cls, d: dict) -> "Staff":
        if d["clef"] not in CLEF_BOTTOM_LINE:
            raise ValueError(f"unknown clef {d['clef']!r}")
        return cls(d["clef"], int(d["system"]), [tuple(map(float, p)) for p in d["samples"]])


@dataclass
class Barline:
    system: int
    x: float


@dataclass
class Column:
    """All notes attacked at one onset (one printed chord)."""

    index: int
    onset: float
    notes: list[Note]                 # attacked here, highest pitch first
    sounding: dict[str, Note]         # voice -> note sounding here (attacked or held)


@dataclass
class Score:
    title: str = ""
    image: Optional[str] = None
    key_fifths: int = 0               # -3 = three flats
    tempo_bpm: float = 80.0           # quarter notes per minute
    chord_duration: float = 2.0       # beats per printed chord (image-derived notes)
    hold_missing_voices: bool = True  # a voice missing from a chord sustains its last note
    group_tolerance: float = 2.0      # staff spaces: noteheads closer than this form one chord
    staves: list[Staff] = field(default_factory=list)
    barlines: list[Barline] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)

    # ---- queries -------------------------------------------------------
    def note(self, note_id: str) -> Note:
        for n in self.notes:
            if n.id == note_id:
                return n
        raise KeyError(note_id)

    def columns(self) -> list[Column]:
        """Group notes by onset; also report which note sounds in each voice."""
        onsets: list[float] = []
        for n in sorted(self.notes, key=lambda n: n.onset):
            if not onsets or n.onset - onsets[-1] > 1e-6:
                onsets.append(n.onset)
        cols = []
        for i, t in enumerate(onsets):
            attacked = sorted((n for n in self.notes if abs(n.onset - t) <= 1e-6),
                              key=lambda n: (n.pitch.midi, n.pitch.diatonic), reverse=True)
            sounding: dict[str, Note] = {}
            for n in self.notes:
                if n.voice in VOICES and n.onset - 1e-6 <= t < n.onset + n.duration - 1e-6:
                    cur = sounding.get(n.voice)
                    if cur is None or n.onset > cur.onset:
                        sounding[n.voice] = n
            cols.append(Column(i, t, attacked, sounding))
        return cols

    @property
    def end_time(self) -> float:
        return max((n.onset + n.duration for n in self.notes), default=0.0)

    # ---- (de)serialisation --------------------------------------------
    def to_dict(self) -> dict:
        return {
            "format": "h2m-score/1",
            "title": self.title,
            "image": self.image,
            "key_fifths": self.key_fifths,
            "tempo_bpm": self.tempo_bpm,
            "chord_duration": self.chord_duration,
            "hold_missing_voices": self.hold_missing_voices,
            "group_tolerance": self.group_tolerance,
            "staves": [s.to_dict() for s in self.staves],
            "barlines": [{"system": b.system, "x": round(b.x, 2)} for b in self.barlines],
            "notes": [n.to_dict() for n in self.notes],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Score":
        return cls(
            title=d.get("title", ""),
            image=d.get("image"),
            key_fifths=int(d.get("key_fifths", 0)),
            tempo_bpm=float(d.get("tempo_bpm", 80.0)),
            chord_duration=float(d.get("chord_duration", 2.0)),
            hold_missing_voices=bool(d.get("hold_missing_voices", True)),
            group_tolerance=float(d.get("group_tolerance", 2.0)),
            staves=[Staff.from_dict(s) for s in d.get("staves", [])],
            barlines=[Barline(int(b["system"]), float(b["x"])) for b in d.get("barlines", [])],
            notes=[Note.from_dict(n) for n in d.get("notes", [])],
        )

    def dumps(self) -> str:
        """JSON with one note / staff / barline per line (readable, diff-friendly)."""
        items = list(self.to_dict().items())
        out = ["{"]
        for i, (k, v) in enumerate(items):
            comma = "," if i < len(items) - 1 else ""
            if isinstance(v, list) and v:
                inner = ",\n".join("  " + json.dumps(x, ensure_ascii=False) for x in v)
                out.append(f" {json.dumps(k)}: [\n{inner}\n ]{comma}")
            else:
                out.append(f" {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}{comma}")
        return "\n".join(out + ["}"]) + "\n"

    def save(self, path: str) -> None:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(self.dumps())
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "Score":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))
