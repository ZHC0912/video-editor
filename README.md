# VidEditor

A desktop non-linear video editor: import media, cut it on a multi-track
timeline, play it back with synchronised audio, export an MP4. Python 3.12,
PySide6, and FFmpeg driven through a filter graph compiler. Offline, with no
service of any kind behind it.

![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![PySide6 6.11](https://img.shields.io/badge/PySide6-6.11-41CD52?logo=qt&logoColor=white)
![FFmpeg vendored](https://img.shields.io/badge/FFmpeg-vendored-007808?logo=ffmpeg&logoColor=white)
![MIT](https://img.shields.io/badge/license-MIT-blue)
![1133 tests](https://img.shields.io/badge/tests-1133%20passing-brightgreen)

<!-- TODO: docs/demo.gif does not exist yet. Record roughly ten seconds: drag a
     clip from the bin onto the timeline, trim its tail, then play it back.
     Drop the file in, then delete the two comment markers around the line
     below and it renders.

![Trimming a clip and playing the timeline back](docs/demo.gif)

-->

## What it does

- Import video and audio by dialog or drag-and-drop, probed in the background
- Arrange clips on a multi-track timeline with filmstrips and waveforms
- Trim, split, move, duplicate and delete, snapped to the frame grid
- Undo and redo every edit, one step per completed gesture
- Play the timeline back with synchronised audio, scrub it, step it by frame
- Export to H.264/AAC MP4 with resolution and quality presets
- Save and reopen projects as JSON, with autosave and crash recovery
- Runs entirely offline; FFmpeg is a local subprocess, never a service

The parts worth reading the code for are below.

---

## How it works

<!-- TODO: docs/timeline.png does not exist yet. Capture the timeline panel
     with two or three clips on the video track showing filmstrip thumbnails,
     an audio clip below showing its waveform, and the playhead partway
     across. Drop the file in, then delete the two comment markers around the
     lines below and it renders.

![The timeline, with filmstrips on the video track and a waveform below](docs/timeline.png)

*Clips carry filmstrips and waveforms, decoded in the background and cached.
The playhead is the same position the preview is showing.*

-->

Three: the timebase, the filter graph compiler, and the playback clock.

### 1. Time is an integer number of ticks, and the number is 120000

**One integer tick is 1/120000 of a second, and that number was chosen so
every broadcast frame rate divides exactly.**

`TICKS_PER_SECOND = 120000`. Every time value in the model, in every command,
and in every signal is an integer count of these. There are exactly three
floats in the codebase — `gain_db`, `pixels_per_second`, and values on their
way out to a display string or an FFmpeg argument — and each is a deliberate
one-way exit from the tick domain.

**Why not 90000.** 90000 is the MPEG-2 and RTP clock, and the obvious choice.
It is wrong here because it does not divide the NTSC rates. At 24000/1001 fps
one frame is `90000 * 1001 / 24000 = 3753.75` ticks, and at 60000/1001 it is
1501.5. A frame duration that lands on a fraction of a tick reintroduces
exactly the accumulating rounding drift an integer timebase exists to remove:
you place a hundred cuts and the hundredth is a frame out.

120000 divides by 1000 and by 24, 25, 30, 50, 60 and 120, so every broadcast
rate lands on a whole number of ticks per frame:

| fps | ticks/frame | | fps | ticks/frame |
|---|---|---|---|---|
| 24 | 5000 | | 24000/1001 | 5005 |
| 25 | 4800 | | 30000/1001 | 4004 |
| 30 | 4000 | | 60000/1001 | 2002 |
| 50 | 2400 | | 120 | 1000 |

Divisibility by 1000 matters separately: `QMediaPlayer` reports and accepts
milliseconds, and a millisecond is exactly 120 ticks, so the one boundary the
application cannot avoid crossing is exact in both directions.

Real files are less tidy than that table. A variable frame rate recording
probes to whatever average ffprobe computed, something like 24249/1000, and
120000 does not divide it at all. So a frame rate is stored as an exact
rational `FrameRate(num, den)` and `ticks_per_frame` returns a `Fraction`.
Nothing anywhere may assume it is a whole number.

→ `core/timebase.py`

### 2. The filter graph compiler: a whole timeline, one FFmpeg invocation

**A multi-track timeline with gaps, trims and per-clip gain becomes one FFmpeg
command, built as pure string construction and tested without ever running
it.**

`core/filtergraph.py` turns a `Project` into `(inputs, filter_complex, maps)`.
No intermediate files, no per-clip render and concat pass: a multi-track edit
with gaps, trims, per-clip gain and a muted track becomes a single `ffmpeg`
command. It is pure string construction — nothing in it runs FFmpeg — which is
why there are nearly 600 lines of tests for it that execute in under a second.

Each track is laid out as segments: every clip, and every gap between clips,
in timeline order, tiling the span with no holes. A clip segment becomes a
`trim` / `setpts` / `scale` / `pad` / `fps` / `format` chain; a gap becomes a
`color` (or `anullsrc`) source of exactly the right duration. Segments are
`concat`ed into one stream per track, and the audio tracks are mixed with
`amix` and padded to the full project duration with `apad`. A source used by
three clips is one input referenced three times.

Ticks become seconds at the last possible moment, and both ends of a span are
snapped to the frame grid *before* the subtraction, so adjacent segments still
tile exactly after quantisation.

Two entry points share all of it. `build_full` produces the export graph;
`build_audio_only` produces the preview audio bed and differs in exactly one
way — the video branch is not built, so files carrying only video never become
inputs. The audio branch is identical in both, down to the trailing pad,
because **the bed is meant to be the export's audio**. If the preview and the
export disagreed about audio, the preview would stop being a preview.

→ `core/filtergraph.py`, `core/render.py`

### 3. Playback: the audio is the clock

**The pre-rendered audio bed is the master clock: the video is corrected to
it, never the other way round.**

The hard problem in an editor's preview is not decoding, it is agreement. Two
streams playing at once will drift, and something has to be right.

Here the audio is right. Every edit touching an audio track invalidates a
pre-rendered WAV of the entire audio timeline, and 500ms after the edits stop
a new one is rendered in the background. During playback **that file's
position is the timeline position**. The video is corrected to it and never
the other way round. A frame arriving late is a frame the viewer might not
notice; audio that stutters, resamples or gaps is immediately obvious.

Consequences worth stating:

- The video players are deliberately silent. There is exactly one audio output
  in the application. A second one would be a second clock with no way to say
  which was right.
- Playback never waits for a bed. Until one exists, or when a project has no
  audio at all, a `QElapsedTimer` drives the position, and the bed takes over
  the moment it lands.
- The drift tolerance is 250ms, deliberately loose. A correction is a seek and
  a seek is visible; correcting a 40ms discrepancy would look worse than the
  discrepancy. Positions the *user* chose — a seek, a scrub, a cut — are
  placed exactly instead.

**Measured: video-to-audio offset after 60 seconds of continuous playback was
−41 ms**, about 1.2 frames at 30fps and well inside the tolerance. Caveat on
that number: it is `QMediaPlayer.position()` minus the bed position, and
`QMediaPlayer.position()` is itself coarse — it updates on its own schedule
and lags the frame actually on screen. −41 ms is the reported offset, not a
measurement of the photons.

**The stage is double buffered.** A cut is not a load. Two `QMediaPlayer`s sit
in a `QStackedWidget`: one plays the current clip while the other is already
loaded and seeked to the first frame of the next. At the cut, the standby is
raised. Loading a file takes hundreds of milliseconds; raising a widget does
not. **Measured: cut times of 1.3 to 13.7 ms** across a four-clip timeline,
every one under a single 33 ms frame, three of the four on the fast path, and
zero black frames mid-playback.

One Qt trap is worth writing down because it cost real time: `setSource` with
a URL that is already open does **not** re-emit `LoadedMedia`, so a seek
issued while waiting for that status is silently dropped and the player stays
on the previous frame. Scrubbing backwards across a split hits it every time,
because every clip of a split is the same file.

→ `ui/playback_controller.py`, `ui/video_stage.py`, `ui/workers/audio_bed_worker.py`

---

## Features

<!-- TODO: docs/export.png does not exist yet. Capture the export dialog with a
     destination filled in and a resolution preset chosen, or the progress
     dialog partway through a render showing the elapsed and remaining line.
     Drop the file in, then delete the two comment markers around the lines
     below and it renders.

![The export dialog, with resolution and quality presets](docs/export.png)

*Resolution and quality presets, an estimated duration, and progress that
reports what FFmpeg actually encoded rather than an interpolated guess.*

-->

- Import media by dialog or by dropping files onto the window. Every file is
  probed on a background thread; the bin fills in as answers arrive.
- Drag from the bin onto a track to place a full-length clip.
- Move, trim, split, duplicate, delete and set the gain of clips. Snapping to
  the frame grid and to clip edges; overlaps are refused, not resolved.
- Undo and redo everything, one entry per completed gesture.
- Play the timeline with audio, scrub it, step it a frame at a time.
- Waveforms and filmstrip thumbnails on the clips, decoded in the background
  and cached.
- Save and open projects as JSON, with autosave, recent files, and recovery
  after a session that did not finish.
- Relink media that has moved, by file or by searching a folder recursively.
- Export with a resolution preset, a quality preset, progress with an
  estimate, a cancel that terminates FFmpeg and deletes the partial file, and
  optional GPU encoding when the machine can actually do it.

---

## Architecture

```
core/     the editor, with no user interface at all
  timebase.py     ticks, frame rates, timecode
  model.py        Project / Track / Clip, and nothing else
  commands.py     every edit, as an undoable command
  filtergraph.py  Project -> an FFmpeg filter graph
  render.py       running FFmpeg
  probe.py        reading a media file
  project_io.py   saving, loading, autosave sidecars
  media_check.py  finding media that has moved
  encoders.py     which encoders this machine can use
  binaries.py     where the vendored FFmpeg is

ui/       everything that draws
  main_window.py  the shell, the menus, the project lifecycle
  timeline/       the scene, the items, the gestures
  workers/        background jobs: probes, thumbnails, waveforms, the audio bed
  playback_controller.py, video_stage.py, preview_panel.py
  export_dialog.py, relink_dialog.py, settings.py
```

### `core/` never imports Qt

Not a style preference: an enforced invariant, with a test that walks the
imports. The point is that the interesting parts of an editor — what an edit
means, what a project is, how a timeline becomes an FFmpeg command — are
decidable without a window. They can be tested in milliseconds, in bulk, with
no event loop, no offscreen platform plugin and no widget lifetimes to manage.
Around 390 of this project's 1133 tests never construct a QApplication, and
they are the ones that cover what an edit actually means.

The dependency goes one way only. `ui/` calls into `core/`; `core/` does not
know `ui/` exists. Anything travelling the other way is a signal or a callback
the UI installs.

Two rules follow from it and are also enforced by tests: every change to the
model goes through a command on the undo stack, and no widget anywhere
mutates a `Track` or a `Clip` directly.

### The edit model on disk

A project is JSON. Source paths are written relative to the project file when
the media sits under the same tree, so a project folder can be copied whole,
and absolute otherwise. Both resolve back to absolute on load.

```json
{
  "schema_version": 2,
  "project": {
    "name": "holiday",
    "width": 1920,
    "height": 1080,
    "frame_rate": {"num": 30, "den": 1},
    "sample_rate": 48000,
    "tracks": [
      {
        "id": "6f1c...",
        "name": "V1",
        "kind": "video",
        "muted": false,
        "clips": [
          {
            "id": "a83b...",
            "src": "media/shot.mp4",
            "src_in": 0,
            "src_out": 480000,
            "timeline_start": 0,
            "gain_db": 0.0
          }
        ]
      }
    ]
  }
}
```

A clip is a span of a source file (`src_in` to `src_out`, in ticks) placed at
`timeline_start`. Its length is implied, never stored, so there is no second
copy of it to disagree. Tracks are sorted by `timeline_start` and validated
non-overlapping on load, so a hand-edited file that overlaps two clips is
rejected rather than half-rendered. `schema_version` is checked exactly: this
build reads 2 and says so about anything else.

---

## Build and run

### You have to supply FFmpeg

**FFmpeg is not in this repository.** Two binaries are needed:

```
bin/ffmpeg.exe
bin/ffprobe.exe
```

They are excluded for two reasons: they are about 100 MB each, which is over
GitHub's per-file limit, and they are GPL where this source is MIT. Download a
build from <https://ffmpeg.org/download.html> (this was developed against the
2026-07-16 gyan.dev *essentials* build for Windows) and drop the two
executables into `bin/`. Nothing resolves FFmpeg through `PATH`: a system
FFmpeg would be an unknown build with unknown codecs, and silently using it
would make the output depend on the machine.

### Running from source

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt

python app.py                  # the application
python app.py holiday.vedit    # with a project open
pytest                         # all tests
pytest -m "not slow"           # skip the ones that actually encode media
```

Python 3.12. Pinned dependencies are in `requirements.txt`.

### Building the shipping folder

```
build.bat
```

which is `pyinstaller build.spec --noconfirm`. The result is
`dist\videditor\`, a one-folder build containing the interpreter, Qt and both
FFmpeg binaries; it needs no Python on the machine that runs it.

One-folder rather than one-file on purpose: a one-file build unpacks its
entire payload — 200 MB of FFmpeg included — into a temporary directory on
every launch, which is slower every single time in exchange for looking tidier
once. `core/binaries.py` resolves the binaries under `sys._MEIPASS` first, so
the same code finds them frozen and from a checkout.

**Size: 354 MiB (371 MB).** FFmpeg is 196 MB of it. Qt is 114 MB after the
excluded modules are dropped. The spec also prunes Qt libraries the PySide6
hook copies whether or not anything imports them (Quick, Qml, Pdf,
VirtualKeyboard — all verified to have no dependents outside their own group),
worth about 17 MB. `opengl32sw.dll` is deliberately kept at 20 MB: nothing
links to it because Qt loads it by name when a machine has no usable OpenGL
driver, and a video editor that will not start on such a machine is worse than
a folder 20 MB larger.

Anyone distributing a packaged build with FFmpeg inside it takes on FFmpeg's
licence obligations. See `LICENSE`.

---

## Known limitations

**One video track.** A scope decision, not an oversight. The exporter refuses
a second video track carrying clips, the "+ Video" button becomes "1 video
track max" when one exists, and the reason is that two video tracks mean
**compositing**: an `overlay` chain, per-clip opacity, blend modes, and a
z-order the model would have to carry. That is a feature, not a relaxed
constraint, and it is the single thing that would lift this limitation.
Everything else is already ready for it — the audio branch mixes any number of
tracks today.

- **No transitions or effects.** Cuts only. The filter graph compiler has the
  shape to grow them; the UI for them does not exist.
- **No scrub audio.** Dragging the playhead moves the picture and leaves the
  audio silent.
- **Frame-accurate scrubbing is only as accurate as `QMediaPlayer`.** Seeks
  land on the nearest decodable position, not always the exact frame. A real
  frame server — a decoder thread and a ring buffer, replacing `QMediaPlayer`
  entirely — is what fixes that, and it is a different project.
- **An unsaved project does not autosave.** The autosave is a sidecar beside
  the project file, so a project that has never been saved has nowhere to put
  one.
- **The audio bed is a whole-timeline render.** Long projects mean a longer
  wait after each audio edit. Debounced to 500ms and cancelled on the next
  edit, but still linear in project length.
- **Hardware encoding needs a recent driver.** The export dialog offers NVENC
  only when the machine can actually use it, which is two questions: whether
  the vendored FFmpeg was built with the encoder (a fact about the build, the
  same everywhere) and whether it opens on this machine's GPU and driver
  (not). Both are asked, the second by encoding a single frame of black to
  nothing on a background thread at startup. On the development machine the
  answer is currently no, instructively: the GPU is there, but the FFmpeg
  build requires NVENC API 13.1 (driver 610.00+) and the installed driver
  provides 13.0. The `-encoders` listing alone would have offered a checkbox
  that fails at the first export.
- **Windows only, in practice.** Nothing in `core/` is platform specific, but
  the binary resolution, the packaging spec and the testing are all Windows.

<!-- TODO: docs/relink.png does not exist yet. Open a project whose media has
     moved, let the relink dialog appear, and capture it with one file located
     and one still missing. Drop the file in, then delete the two comment
     markers around the lines below and it renders.

![The relink dialog, listing missing source files](docs/relink.png)

*Media that has moved is found again by file or by searching a folder.
Unresolved clips stay on the timeline, hatched in red, and are skipped on
export rather than failing it.*

-->
