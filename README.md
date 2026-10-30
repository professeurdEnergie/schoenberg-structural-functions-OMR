# schoenberg-structural-functions-OMR

Turns the closed-score SATB examples in Schoenberg's *Structural Functions of Harmony* into
four-track MIDI. The Python package is `h2m` (harmony to MIDI).

You click the noteheads on a photo or scan of an example. The program does the geometry: it
finds the staff lines, snaps each click to a line or space, and applies the key signature and
accidentals (an accidental lasts until the barline). It then groups the noteheads into chords,
labels S/A/T/B by pitch order and flags likely mistakes. Everything goes into one symbolic
score (JSON), which is exported as a four-track MIDI file.

This version has no OMR (optical music recognition) step, on purpose. You have to check every
note of an OMR result anyway. Accidental errors give wrong pitches that still look plausible,
and the Roman numerals, circled letters, `+)` marks, brackets, diagonal lines and parenthesised
notes in these examples are noise to an OMR engine. When you click, you only touch noteheads
and the markup is never read. An OMR or notehead-detection step can be added later as a
pre-fill: it would create notes with `confidence < 1` in the same model, and you would confirm
them in the same editor.

## Install (Python 3.9+)

From the repository root:

```sh
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Commands

| command | does |
|---|---|
| `python -m h2m check score.json` | chord table (S/A/T/B per chord, `~` = held) + flags |
| `python -m h2m export score.json [-o out.mid] [--split] [--program N]` | four-track MIDI; `--split` also writes `out_S.mid` ... `out_B.mid` |
| `python -m h2m detect photo.jpg --debug overlay.png` | staff/barline detection only, with an overlay image to inspect |
| `python -m h2m serve photo.jpg --key -3` | editor at http://127.0.0.1:8765, autosaves `photo.h2m.json` |

The MIDI file has track 0 for the title and tempo, then **Soprano, Alto, Tenor and Bass**
tracks on channels 1-4 (GM church organ by default, slightly panned). Mute or solo the tracks
in any DAW or in MuseScore. To get one voice on its own without a DAW, use `--split`, or mute
voices in the editor before you click *Download MIDI*.

## Trying it on an excerpt

1. **MIDI export without an image:**
   `python -m h2m check examples/demo_cminor.json`, then
   `python -m h2m export examples/demo_cminor.json --split`. You should see 7 chords with
   Fb4, Bbb4 and Db5 in chord 3 and "No problems found". Open `examples/demo_cminor.mid` and
   check that there are four named tracks.
2. **Staff detection:** `python -m h2m detect excerpt.png --debug excerpt_debug.png`. You
   should get 2 staves (treble, bass) and one barline per measure line. Red and blue lines in
   the overlay should sit on the printed staff lines. Small scans work (a staff spacing of
   about 8 px), but zoom to 300-400 % in the editor. A phone photo of one example gives 20 px
   or more, and then the clicks are easy. If you can, use a document-scanner app (it flattens
   the page). The detector handles a few degrees of tilt and gentle page curve.
3. **Editor:** `python -m h2m serve excerpt.png --key -3` (three flats).
   - For each chord, click the 3 treble noteheads and the bass notehead. After each click,
     check the pitch label. When a notehead has a printed accidental, press `1`-`5`
     (double flat, flat, natural, sharp, double sharp) while it is selected.
   - Chords that contain a second (displaced noteheads): click both noteheads. They are still
     grouped as one chord.
   - One notehead shared by two voices (unison): select it and press `d`.
   - Parenthesised notes are optional. Click them only if you want them to sound. The flags
     will show how they change the chord sizes.
   - Check the dashed barlines too. Accidentals reset at each one, so a missed or extra
     barline changes pitches. `Shift`+click adds or removes a barline.
   - `Space` plays the whole excerpt. Each edit plays the edited chord, and the S/A/T/B
     checkboxes mute voices. Fix a note with `Up`/`Down` (staff step), an accidental key, or
     `Enter` (type a pitch). The other keys are listed in the side panel.
   - *Download MIDI*, or `python -m h2m export excerpt.h2m.json`.

## Score model (`*.h2m.json`)

Each note has `pitch` (spelled, so `Bbb4` and `A4` stay different), `onset`/`duration` (in
beats), `voice`, `confidence` and `loc` (`x`, `y`, `staff`, `step`: 0 = bottom line,
1 = first space ...). It also has the printed `accidental` (`bb b n # x`) and `voice_locked`
(a voice you set by hand).

For notes with a `loc`, the pitch, onset, voice and duration are worked out from `step`,
`accidental`, `key_fifths`, the barlines and the x order. If you edit those fields, `check`
and `export` recompute the derived ones. Notes without a `loc` are used as written
(`examples/demo_cminor.json` is an example). A voice that has no note in a printed chord
holds its previous note (`hold_missing_voices`).

## Flags

- **error**: more than 4 notes in a chord, a note with no voice, two notes on one voice,
  a pitch outside the MIDI range.
- **warning**: fewer than 4 notes (the flag says which voice is held or silent); a voice out of
  range (S C4-A5, A G3-D5, T C3-A4, B E2-D4); crossed voices; more than an octave S-A or A-T;
  a leap of more than an octave; a click halfway between a line and a space; a chord whose
  letter names are not stacked thirds. That last check catches most noteheads snapped one step
  off.

## Not yet

- Every chord gets the same duration (`chord_duration`). Notes of different lengths are only
  covered by "hold missing voices".
- There is no manual staff calibration if detection fails. Crop the image, or take a flatter photo.
- No automatic notehead proposals, no OMR pre-fill, no MusicXML export (music21 would be the
  natural tool for that).
