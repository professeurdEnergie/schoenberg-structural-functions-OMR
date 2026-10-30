"""Command line: check / export / detect / serve."""
from __future__ import annotations

import argparse
import os
import sys

from .model import VOICES, Score


def _table(score: Score) -> str:
    cols = score.columns()
    width = max([5] + [len(str(n.pitch)) + 1 for n in score.notes])
    head = "    " + "".join(f"{c.index + 1:>{width}}" for c in cols)
    rows = [head]
    for v in VOICES:
        cells = []
        for c in cols:
            attacked = [n for n in c.notes if n.voice == v]
            if attacked:
                cells.append(str(attacked[0].pitch))
            elif v in c.sounding:
                cells.append("~")  # held from the previous chord
            else:
                cells.append("-")
        rows.append(f"{v:<4}" + "".join(f"{x:>{width}}" for x in cells))
    unassigned = [str(n.pitch) for n in score.notes if n.voice not in VOICES]
    if unassigned:
        rows.append("unassigned: " + " ".join(unassigned))
    return "\n".join(rows)


def _report(score: Score) -> int:
    from .validate import validate
    flags = validate(score)
    print(f"{score.title or '(untitled)'}: key {score.key_fifths:+d}, {len(score.columns())} chords, "
          f"{len(score.notes)} notes, tempo {score.tempo_bpm:g}")
    print(_table(score))
    if not flags:
        print("No problems found.")
    for f in flags:
        print(f"  {f.level.upper():7} {f.code:15} {f.message}")
    return sum(f.level == "error" for f in flags)


def _load(path: str) -> Score:
    """Load and re-derive pitch/onset/voice of image-located notes, so hand edits to
    key_fifths, accidentals, steps or barlines in the JSON take effect."""
    from .interpret import interpret
    score = Score.load(path)
    interpret(score)
    return score


def cmd_check(a) -> int:
    return 1 if _report(_load(a.score)) else 0


def cmd_export(a) -> int:
    from .midi_export import write_midi, write_split
    score = _load(a.score)
    errors = _report(score)
    out = a.output or os.path.splitext(a.score)[0] + ".mid"
    write_midi(score, out, program=a.program)
    print(f"wrote {out} (tracks: conductor + {', '.join(VOICES)})")
    if a.split:
        for p in write_split(score, os.path.splitext(out)[0], program=a.program):
            print(f"wrote {p}")
    return 1 if errors and a.strict else 0


def cmd_detect(a) -> int:
    from .staff import analyse, debug_overlay, load_image
    img = load_image(a.image)
    det = analyse(img)
    print(f"{a.image}: {img.size[0]}x{img.size[1]} px, skew {det.skew_deg:+.2f} deg, {len(det.staves)} staves")
    for i, st in enumerate(det.staves):
        print(f"  staff {i}: {st.clef:6} system {st.system}  spacing {st.spacing:.1f} px  "
              f"x {st.x_range[0]:.0f}-{st.x_range[1]:.0f}")
    print(f"  barlines: {', '.join(f'{b.x:.0f}' for b in det.barlines) or 'none'}")
    if det.staves and min(s.spacing for s in det.staves) < 10:
        print("  note: staff spacing < 10 px - usable, but zoom in the editor or use a higher-resolution photo")
    if a.debug:
        debug_overlay(img, det).save(a.debug)
        print(f"wrote {a.debug}")
    return 0 if det.staves else 1


def cmd_serve(a) -> int:
    from .server import serve
    from .session import Session
    score_path = a.score or os.path.splitext(a.image)[0] + ".h2m.json"
    session = Session(a.image, score_path, a.key)
    if not session.score.staves:
        print("warning: no staves detected - crop the image to the excerpt, or use a flatter/brighter photo",
              file=sys.stderr)
    serve(session, a.host, a.port)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m h2m", description="Harmony excerpt -> SATB MIDI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("check", help="print the chord table and validation flags")
    s.add_argument("score")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("export", help="write a four-track MIDI file")
    s.add_argument("score")
    s.add_argument("-o", "--output")
    s.add_argument("--split", action="store_true", help="also write one file per voice")
    s.add_argument("--program", type=int, default=19, help="GM program 0-127 (default 19, church organ)")
    s.add_argument("--strict", action="store_true", help="exit 1 if validation reports errors")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("detect", help="detect staves/barlines in an image")
    s.add_argument("image")
    s.add_argument("--debug", help="write an overlay PNG of what was detected")
    s.set_defaults(func=cmd_detect)

    s = sub.add_parser("serve", help="open the click-to-place editor for an image")
    s.add_argument("image")
    s.add_argument("--score", help="score JSON (default: <image>.h2m.json, created if missing)")
    s.add_argument("--key", type=int, help="key signature in fifths, e.g. -3 for three flats")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_serve)

    a = p.parse_args(argv)
    return a.func(a)
