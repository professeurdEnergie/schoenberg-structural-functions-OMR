"""Editing session: a Score bound to an image, mutated by small operations.

Every operation re-runs interpretation + validation and autosaves, so the JSON
on disk, the overlay and the playback always agree.
"""
from __future__ import annotations

import copy
import io
import os
import threading
from typing import Optional

from .interpret import interpret, locate, step_for_pitch
from .model import ACCIDENTALS, CLEF_BOTTOM_LINE, VOICES, Barline, ImageLoc, Note, Pitch, Score
from .staff import analyse, load_image
from .validate import validate


class Session:
    def __init__(self, image_path: str, score_path: str, key_fifths: Optional[int] = None):
        self.image_path = image_path
        self.score_path = score_path
        self.image = load_image(image_path)
        buf = io.BytesIO()
        self.image.save(buf, format="PNG")  # orientation-normalised copy for the browser
        self.image_png = buf.getvalue()
        self.lock = threading.Lock()
        self.history: list[dict] = []
        if os.path.exists(score_path):
            self.score = Score.load(score_path)
        else:
            det = analyse(self.image)
            self.score = Score(title=os.path.splitext(os.path.basename(image_path))[0],
                               image=os.path.basename(image_path),
                               staves=det.staves, barlines=det.barlines)
        if key_fifths is not None:
            self.score.key_fifths = key_fifths
        self._refresh()

    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        interpret(self.score)
        self.score.save(self.score_path)

    def state(self) -> dict:
        sc = self.score
        cols = []
        for c in sc.columns():
            xs = [n.loc.x for n in c.notes if n.loc]
            cols.append({"index": c.index, "onset": c.onset,
                         "x": sum(xs) / len(xs) if xs else None,
                         "notes": [n.id for n in c.notes],
                         "sounding": {v: n.id for v, n in c.sounding.items()}})
        return {
            "score": sc.to_dict(),
            "flags": [f.to_dict() for f in validate(sc)],
            "columns": cols,
            "staff_spacing": [st.spacing for st in sc.staves],
            "image_size": list(self.image.size),
            "undo_depth": len(self.history),
        }

    def apply(self, op: dict) -> dict:
        with self.lock:
            kind = op.get("op")
            if kind == "undo":
                if self.history:
                    self.score = Score.from_dict(self.history.pop())
                    self._refresh()
                return {"selected": op.get("id")}
            snapshot = self.score.to_dict()
            result = getattr(self, f"_op_{kind}")(op)
            self.history.append(snapshot)
            del self.history[:-200]
            self._refresh()
            return result or {}

    # ---- operations ---------------------------------------------------
    def _op_add(self, op):
        hit = locate(self.score, float(op["x"]), float(op["y"]))
        if hit is None:
            raise ValueError("click is not near a staff")
        staff, step, conf = hit
        n = Note(Pitch("C", 0, 4), loc=ImageLoc(float(op["x"]), float(op["y"]), staff, step),
                 confidence=conf, accidental=op.get("accidental"))
        self.score.notes.append(n)
        return {"selected": n.id}

    def _op_delete(self, op):
        self.score.notes = [n for n in self.score.notes if n.id != op["id"]]
        return {"selected": None}

    def _op_move(self, op):  # one staff step up (+1) or down (-1)
        n = self.score.note(op["id"])
        if n.loc is None:
            n.pitch = Pitch.from_diatonic(n.pitch.diatonic + int(op["delta"]), n.pitch.alter)
        else:
            n.loc.step += int(op["delta"])
        n.confidence = 1.0  # a human looked at it
        return {"selected": n.id}

    def _op_accidental(self, op):
        acc = op.get("accidental")
        if acc is not None and acc not in ACCIDENTALS:
            raise ValueError(f"unknown accidental {acc!r}")
        n = self.score.note(op["id"])
        if n.loc is None:
            n.pitch = Pitch(n.pitch.step, ACCIDENTALS.get(acc, 0), n.pitch.octave)
        n.accidental = acc
        return {"selected": n.id}

    def _op_voice(self, op):
        n = self.score.note(op["id"])
        v = op.get("voice")
        if v is None:
            n.voice_locked = False
        elif v in VOICES:
            n.voice, n.voice_locked = v, True
        else:
            raise ValueError(f"unknown voice {v!r}")
        return {"selected": n.id}

    def _op_duplicate(self, op):
        """A single printed notehead shared by two voices (unison)."""
        n = self.score.note(op["id"])
        d = copy.deepcopy(n)
        d.id = Note(Pitch("C", 0, 4)).id
        d.voice_locked = False
        if d.loc:
            sp = self.score.staves[d.loc.staff].spacing
            d.loc = ImageLoc(d.loc.x + 0.35 * sp, d.loc.y, d.loc.staff, d.loc.step)
        self.score.notes.append(d)
        return {"selected": d.id}

    def _op_set_pitch(self, op):
        """Type a pitch directly ('Bbb4').  For image notes this moves the notehead and
        records the matching printed accidental, keeping the model consistent."""
        n = self.score.note(op["id"])
        p = Pitch.parse(op["pitch"])
        if n.loc is None:
            n.pitch = p
        else:
            n.loc.step = step_for_pitch(self.score, n.loc.staff, p)
            n.accidental = None  # prefer "no printed accidental" if key/measure context gives p
            interpret(self.score)
            if n.pitch != p:
                n.accidental = {-2: "bb", -1: "b", 0: "n", 1: "#", 2: "x"}[p.alter]
        n.confidence = 1.0
        return {"selected": n.id}

    def _op_confirm(self, op):
        self.score.note(op["id"]).confidence = 1.0
        return {"selected": op["id"]}

    def _op_barline(self, op):
        x, y = float(op["x"]), float(op["y"])
        hit = locate(self.score, x, y)
        if hit is None:
            raise ValueError("click is not near a staff")
        system = self.score.staves[hit[0]].system
        sp = self.score.staves[hit[0]].spacing
        near = [b for b in self.score.barlines if b.system == system and abs(b.x - x) <= sp]
        if near:
            self.score.barlines.remove(near[0])
        else:
            self.score.barlines.append(Barline(system, x))
        return {}

    def _op_settings(self, op):
        sc = self.score
        if "key_fifths" in op:
            sc.key_fifths = max(-7, min(7, int(op["key_fifths"])))
        if "tempo_bpm" in op:
            sc.tempo_bpm = max(10.0, min(400.0, float(op["tempo_bpm"])))
        if "chord_duration" in op:
            sc.chord_duration = max(0.25, float(op["chord_duration"]))
        if "hold_missing_voices" in op:
            sc.hold_missing_voices = bool(op["hold_missing_voices"])
        if "title" in op:
            sc.title = str(op["title"])
        if "clef" in op:  # {"clef": {"staff": 1, "clef": "bass"}}
            if op["clef"]["clef"] not in CLEF_BOTTOM_LINE:
                raise ValueError(f"unknown clef {op['clef']['clef']!r}")
            sc.staves[int(op["clef"]["staff"])].clef = op["clef"]["clef"]
        return {}

    def _op_clear(self, op):
        self.score.notes = []
        return {"selected": None}
