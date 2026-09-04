# VidEditor: Phase Plan and Claude Code Prompts

Revision 2. Desktop video editor. Python 3.12 + PySide6 + bundled FFmpeg. Fully offline.

**Rule for every phase:** `core/` never imports Qt. If you catch Claude Code adding `from PySide6` to anything under `core/`, stop it.

---

## What changed from revision 1

Three limitations previously accepted as "good enough" are now solved properly.

| Was | Now |
|---|---|
| Float seconds, rounding error accumulates | Integer ticks in a rational timebase, zero drift by construction |
| Audio played clip by clip, drifts against video | Whole audio timeline pre-rendered to one WAV, then used as the master clock |
| Visible hitch at every cut | Double buffered video players, next clip preloaded and paused on its in-frame |

Net cost: roughly one extra week. Phase 5 gets simpler, not harder, because all the drift correction logic is deleted.

---

## Locked decisions

| Decision | Choice |
|---|---|
| Time units | Integer ticks, `TICKS_PER_SECOND = 120000` |
| Frame rate storage | Rational pair `fps_num` / `fps_den`, never a float |
| Project settings | Fixed `width`, `height`, `frame_rate`, `sample_rate` set at creation |
| Export strategy | Always re-encode. Never stream copy. |
| Preview audio | Single pre-rendered WAV of the whole audio timeline |
| Master clock | The audio player's position. Video is corrected to it. |
| Video preview | Two players in a `QStackedWidget`, next clip preloaded |
| Undo granularity | One command per completed gesture, pushed on mouse release |
| FFmpeg source | LGPL build from gyan.dev, vendored into `bin/` |
| Video tracks | Exactly one in v1. No compositing. |

### Why 120000 ticks per second

I said 90000 earlier, which is the MPEG standard value. It is wrong for this use. 90000 does not divide evenly by the NTSC rates, so 23.976fps and 59.94fps projects land on fractional ticks.

120000 divides exactly for every rate you will meet:

| fps | ticks per frame |
|---|---|
| 24 | 5000 |
| 25 | 4800 |
| 30 | 4000 |
| 50 | 2400 |
| 60 | 2000 |
| 120 | 1000 |
| 23.976 (24000/1001) | 5005 |
| 29.97 (30000/1001) | 4004 |
| 59.94 (60000/1001) | 2002 |

Every frame boundary is an exact integer tick. Python ints are unbounded, so there is no overflow ceiling. Floats appear only at the UI display boundary and in the FFmpeg command string.

---

## Phase 0: Scaffold, model, probe

### Setup commands (run these yourself first)

```bash
cd C:\Users\User\Documents\GitHub
mkdir videditor
cd videditor
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install PySide6 pydantic numpy Pillow pytest
mkdir bin
```

Download `ffmpeg-release-essentials.zip` from gyan.dev, extract, copy `ffmpeg.exe` and `ffprobe.exe` into `videditor\bin\`.

### Prompt

```
I am building a desktop video editor in Python 3.12 with PySide6. This is Phase 0
of 6. Do not write any UI code in this phase.

Working directory: C:\Users\User\Documents\GitHub\videditor
FFmpeg binaries are already vendored at bin\ffmpeg.exe and bin\ffprobe.exe

Build the pure-Python core with no Qt dependency whatsoever.

FILES TO CREATE:
  core/__init__.py
  core/timebase.py
  core/binaries.py
  core/model.py
  core/probe.py
  core/project_io.py
  tests/test_timebase.py
  tests/test_model.py
  tests/test_project_io.py
  pyproject.toml
  .gitignore

=== core/timebase.py ===

This module is the foundation. Get it exactly right.

    TICKS_PER_SECOND = 120000

    @dataclass(frozen=True)
    class FrameRate:
        num: int
        den: int
        # 30fps is FrameRate(30, 1). 29.97 is FrameRate(30000, 1001).

        @property
        def as_float(self) -> float            # display only
        @property
        def ticks_per_frame(self) -> Fraction  # Fraction(TICKS_PER_SECOND * den, num)

        @classmethod
        def from_ffprobe(cls, r_frame_rate: str) -> "FrameRate"
            # parses "30000/1001", reduces with math.gcd

    Free functions, integer in and integer out:
      seconds_to_ticks(s: float) -> int          # round(), import boundaries only
      ticks_to_seconds(t: int) -> float          # display and ffmpeg args only
      frames_to_ticks(f: int, rate: FrameRate) -> int
      ticks_to_frames(t: int, rate: FrameRate) -> int
      snap_to_frame(t: int, rate: FrameRate) -> int
      ticks_to_timecode(t: int, rate: FrameRate) -> str   # "HH:MM:SS:FF"

    ticks_per_frame returns a Fraction, not a float. For every rate below it happens
    to be a whole number, but do not encode that assumption in the type.

    Assert in a test that ticks_per_frame is integral for: 24, 25, 30, 50, 60, 120,
    24000/1001, 30000/1001, 60000/1001.

    Module docstring must explain why 120000 was chosen over the MPEG-standard 90000.

=== core/binaries.py ===

    resolve_binary(name: str) -> Path
      Returns the path to ffmpeg.exe or ffprobe.exe. Handles BOTH the dev layout
      (bin/ next to the package root) and PyInstaller (sys._MEIPASS). Never relies on
      system PATH. probe.py and render.py both use this. Write it once, here.

=== core/model.py ===

pydantic v2. ALL time fields are int ticks. There must be no float time field
anywhere in this file. If you find yourself wanting one, stop and tell me.

  class MediaInfo:
      path: Path
      duration_ticks: int
      width: int | None
      height: int | None
      frame_rate: FrameRate | None
      has_video: bool
      has_audio: bool
      sample_rate: int | None

  class Clip:
      id: str                      # uuid4 hex via default_factory
      src: Path
      src_in: int                  # ticks into the source file
      src_out: int
      timeline_start: int          # ticks on the timeline
      gain_db: float = 0.0         # a float, but it is not a time value
      # property duration -> src_out - src_in
      # property timeline_end -> timeline_start + duration

  class Track:
      id: str
      name: str
      kind: Literal["video", "audio"]
      clips: list[Clip]
      muted: bool = False

  class Project:
      name: str
      width: int = 1920
      height: int = 1080
      frame_rate: FrameRate = FrameRate(30, 1)
      sample_rate: int = 48000
      tracks: list[Track]
      # property duration -> max timeline_end across all tracks, else 0
      # property has_audio -> any unmuted audio track holding at least one clip

VALIDATION (raise ValueError, never silently correct):
  - src_out > src_in
  - timeline_start >= 0
  - clips within one track must not overlap
  - a validator keeps each track's clip list sorted by timeline_start

Free functions in model.py:
  - clip_at(track, t: int) -> Clip | None
  - next_clip_after(track, t: int) -> Clip | None   # Phase 5 preloading needs this
  - gaps_in(track, until: int) -> list[tuple[int, int]]   # includes a leading gap

=== core/probe.py ===

  - probe(path: Path) -> MediaInfo
  - Calls ffprobe via resolve_binary with:
      -v quiet -print_format json -show_format -show_streams
  - Parse r_frame_rate through FrameRate.from_ffprobe
  - Convert ffprobe's float duration into ticks once, at this boundary
  - Raise ProbeError with ffprobe stderr attached on non-zero exit
  - Handle video-only, audio-only, and combined files

=== core/project_io.py ===

  - save(project, path) / load(path) -> Project
  - JSON with "schema_version": 2
  - Store source paths RELATIVE to the project file directory when the media sits
    under the same tree, absolute otherwise. Resolve back on load.
  - FrameRate serialises as {"num": 30000, "den": 1001}

TESTS: overlap rejection, gap computation including leading gaps, save/load round trip
preserving every field exactly, ticks_per_frame integrality for the nine rates above,
timecode formatting for 29.97 and 25fps, probe against a fixture generated by
ffmpeg lavfi testsrc inside a pytest fixture. Do not commit a binary fixture.

ACCEPTANCE:
  - pytest passes
  - grep for "PySide6" under core/ returns nothing
  - grep for "float" in core/model.py matches only gain_db

STOP after this phase. Report anything you had to assume.
```

---

## Phase 1: Render pipeline and audio bed

Deliberately before any UI. If the model cannot export, the timeline is decorating a broken foundation.

### Prompt

```
Phase 1 of 6. Continue the videditor project. Still no UI code.

Build the export pipeline AND the audio bed renderer that Phase 5 depends on.

FILES TO CREATE:
  core/filtergraph.py
  core/render.py
  tests/test_filtergraph.py

=== core/filtergraph.py ===

Two public functions sharing one internal builder:

    build_full(project) -> tuple[list[str], str, list[str]]
        # (input_args, filter_complex, map_args)

    build_audio_only(project) -> tuple[list[str], str, list[str]]
        # same, video branch omitted entirely

Do not duplicate the builder. Write one _build(project, include_video: bool).

BUILDER RULES:

1. Collect distinct source files across all tracks into an ordered input list. Each
   becomes one -i argument. The same file used by three clips is ONE input,
   referenced three times.

2. All time values entering the filter string convert from ticks at the last possible
   moment, via ticks_to_seconds, formatted to 6 decimal places. Snap to frame
   boundaries with snap_to_frame BEFORE converting. Snapping happens in one place.

3. Per VIDEO clip:
     [{idx}:v]trim=start={in}:end={out},
     setpts=PTS-STARTPTS,
     scale={W}:{H}:force_original_aspect_ratio=decrease,
     pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,
     fps={num}/{den},
     format=yuv420p,
     setsar=1[v{n}]

4. Per video GAP:
     color=c=black:s={W}x{H}:d={gap_sec}:r={num}/{den},format=yuv420p,setsar=1[v{n}]

5. Concat video segments in timeline order:
     [v0][v1][v2]concat=n=3:v=1:a=0[vout]

6. Per AUDIO clip:
     [{idx}:a]atrim=start={in}:end={out},
     asetpts=PTS-STARTPTS,
     aformat=sample_rates={SR}:channel_layouts=stereo,
     volume={gain_db}dB[a{n}]

7. Per audio GAP:
     anullsrc=r={SR}:cl=stereo,atrim=duration={gap_sec},asetpts=PTS-STARTPTS[a{n}]

8. Concat per audio track, then mix tracks:
     [atrack0][atrack1]amix=inputs=2:normalize=0[aout]
   With exactly one audio track, alias via anull instead of amix.

9. Skip muted tracks entirely.

10. CRITICAL for the audio bed: build_audio_only must pad the mixed audio to the FULL
    project duration, not merely to the last audio clip. Append
    apad=whole_dur={project_duration_sec} before [aout]. Phase 5 uses the WAV as the
    master clock, so it must span the whole timeline including trailing silence.

EDGE CASES, handle explicitly:
  - zero tracks -> RenderError with a clear message
  - video track but no audio at all -> build_full omits the audio map entirely, and
    build_audio_only raises NoAudioError (Phase 5 catches it and falls back to a
    wall clock)
  - audio but no video -> RenderError, not supported in v1
  - single clip, no gaps -> concat with n=1 is valid, keep the code path uniform

=== core/render.py ===

  render(project, out_path, cancel: threading.Event, crf: int = 20,
         on_progress=None) -> None
    ffmpeg -y {inputs} -filter_complex "{fc}" {maps}
      -c:v libx264 -preset medium -crf {crf} -pix_fmt yuv420p
      -c:a aac -b:a 192k
      {out_path}

  render_audio_bed(project, out_wav, cancel, on_progress=None) -> None
    ffmpeg -y {inputs} -filter_complex "{fc}" -map "[aout]"
      -c:a pcm_s16le -ar {sample_rate} -ac 2
      {out_wav}
    Uses build_audio_only. This must be FAST. Do not re-encode video, do not even
    reference the video streams.

  Both:
    - subprocess.Popen, read stderr line by line
    - parse "time=HH:MM:SS.ss" for progress, call on_progress(done_sec, total_sec)
    - poll the cancel Event each line; on set, terminate and delete the partial file
    - non-zero exit raises RenderError carrying the last 40 lines of stderr

TESTS: assert on the generated STRING, not on rendered output. Cover single clip, two
clips back to back, two clips with a gap, a leading gap, video plus one audio track,
video plus two audio tracks, muted track exclusion, deduplicated inputs, apad present
in build_audio_only and absent in build_full.

Add one @pytest.mark.slow integration test: build a two-clip project from lavfi
fixtures, render it, probe the output, assert duration within one frame of
project.duration.

ACCEPTANCE:
  - A 15-line script constructing a Project by hand and calling render() produces a
    playable MP4 of correct duration
  - render_audio_bed on a 60-second project completes in under 3 seconds

STOP. Show me the exact filter_complex from build_audio_only for a project with two
video clips and one music bed, so I can eyeball the apad.
```

---

## Phase 2: Application shell and preview scaffolding

### Prompt

```
Phase 2 of 6. Now we add UI. PySide6 only, no QML.

Build the shell and the preview panel's structure. The panel plays a single file for
now; Phase 5 turns it into a timeline player. Build it so that upgrade is a swap of
one controller, not a rewrite.

FILES TO CREATE:
  ui/__init__.py
  ui/main_window.py
  ui/preview_panel.py
  ui/video_stage.py
  ui/single_player_controller.py
  ui/theme.py
  ui/format_utils.py
  app.py

=== ui/theme.py ===
Dark theme as a Qt stylesheet.
  Background #1a1a1e, panels #232329, elevated #2a2a32, borders #34343c
  Text #e4e4e8, muted #8a8a95
  Accent #4f8fff for selection and the playhead
  Warning #d98c3a, error #d95a5a
  Segoe UI 9pt, Cascadia Mono for all timecode fields
  Corner radius never above 4px. Clear contrast steps between panel levels so the
  timeline reads as its own surface.

=== ui/video_stage.py ===
This is the double-buffered video surface. Build it now, use it fully in Phase 5.

  class VideoStage(QWidget)
    - A QStackedWidget holding TWO QVideoWidget instances, each with its own
      QMediaPlayer. Neither gets a QAudioOutput: video players are SILENT. All audio
      comes from the separate bed player in Phase 5.
    - A third stacked page: a plain black QWidget, shown during timeline gaps.

    Public API:
      show_black()
      active_player() -> QMediaPlayer
      standby_player() -> QMediaPlayer
      present_standby()            # raise the standby widget, swap the roles
      preload(path, position_ms)   # load into standby, seek, pause, do not show
      signal standby_ready()       # standby reached LoadedMedia and its seek settled

    present_standby() must be cheap: setCurrentIndex plus play on the newly active
    player, nothing else. All decoding work belongs in preload.

=== ui/preview_panel.py ===
  class PreviewPanel(QWidget)
    - Embeds a VideoStage
    - Transport bar: skip-to-start, frame-back, play/pause, frame-forward, skip-to-end
    - QSlider scrubber
    - Cascadia Mono timecode label using ticks_to_timecode
    - Volume slider
    - A muted-text status line, empty for now, used in Phase 5

    Public API (Phase 5 drives all of this):
      set_project(project)
      position_ticks() -> int
      seek(ticks: int)
      play() / pause() / is_playing() -> bool
      signal position_changed(int)

    Frame step buttons move by exactly frames_to_ticks(1, project.frame_rate).

=== ui/single_player_controller.py ===
  For this phase only. Plays one imported file end to end and feeds PreviewPanel's
  signals. Separate file so Phase 5 replaces it without touching preview_panel.py.

=== ui/main_window.py ===
  - Menu bar: File (New, Open, Save, Save As, Import Media, Export, Exit)
  - Central QSplitter vertical:
      top: horizontal QSplitter -> [media bin] | [preview panel]
      bottom: timeline placeholder QFrame
  - Status bar: project name, duration timecode, frame rate

  File actions:
    - Import Media calls core.probe.probe(), appends to an in-memory list rendered in
      a QListWidget with duration and resolution as secondary text
    - Open / Save call core.project_io
    - Export opens a save dialog, then runs core.render.render() on a QThread with a
      modal QProgressDialog wired to on_progress and a Cancel that sets the
      threading.Event. NEVER call render on the GUI thread.

=== ui/format_utils.py ===
Display-side conversion only. ticks_to_timecode wrappers, human-readable durations,
file size formatting. This is the ONLY place in ui/ allowed to turn ticks into strings.

CONSTRAINT: ui/ may import core/. core/ must never import ui/ or PySide6.

ACCEPTANCE:
  - python app.py opens a dark window
  - Import an MP4, double-click it, it plays
  - Frame step moves exactly one frame at 29.97fps as well as at 30fps
  - Export on a hand-built project shows real progress and cancels cleanly

STOP.
```

---

## Phase 3: Timeline rendering and background workers

### Prompt

```
Phase 3 of 6. Build the timeline view, READ ONLY. It renders the model. No drag, no
trim, no delete this phase.

FILES TO CREATE:
  ui/timeline/__init__.py
  ui/timeline/timeline_view.py
  ui/timeline/timeline_scene.py
  ui/timeline/clip_item.py
  ui/timeline/ruler_item.py
  ui/timeline/playhead_item.py
  ui/workers/__init__.py
  ui/workers/thumbnail_worker.py
  ui/workers/waveform_worker.py
  ui/workers/audio_bed_worker.py

ARCHITECTURE: QGraphicsView + QGraphicsScene.

Coordinate mapping lives in ONE place, on the scene:
    pixels_per_second: float
    ticks_to_x(t: int) -> float
    x_to_ticks(x: float) -> int          # rounds, then snaps to frame
No other file may do this arithmetic. Get this right now, it will not be fixed later.

LAYOUT:
  - Time ruler across the top, tick interval chosen for the zoom level
    (1s, 5s, 10s, 30s, 1m), labelled MM:SS
  - Video lanes 72px, audio lanes 56px
  - Track header column on the left, 120px fixed, does not scroll horizontally.
    Track name plus a mute toggle.
    The "add video track" affordance is disabled when a video track already exists.
    Tooltip: "Multiple video tracks require compositing, not supported in this version."
    Audio tracks are unlimited.
  - Playhead: 2px accent line spanning all lanes, drawn above clips

ClipItem (QGraphicsRectItem subclass):
  - 3px rounded rect. Video #2d4a6b, audio #2d5a4a.
  - Elided filename top-left inside the rect
  - Holds the Clip ID string, NEVER a Clip object. Always look up by id.
  - Video clips paint a filmstrip of thumbnails
  - Audio clips paint a waveform mirrored about the vertical centre

=== WORKERS ===

Use QRunnable on a shared QThreadPool. Justify in a comment: these are many short
independent jobs, exactly what a pool is for, and a QThread per job would thrash.

thumbnail_worker: for evenly spaced timestamps across a clip, run
  ffmpeg -ss {t} -i {src} -frames:v 1 -vf scale=-1:64 -f image2pipe -vcodec png -
Cache by (src, rounded_tick) in an LRU dict capped at 500 entries.

waveform_worker: run
  ffmpeg -i {src} -ac 1 -filter:a aresample=8000 -map 0:a -c:a pcm_s16le -f data -
Read the raw stream, compute per-pixel min/max peaks with numpy. Cache the FULL-FILE
peak array per source, then slice per clip. Do not re-decode per clip.

audio_bed_worker: this is the one Phase 5 needs.
  - Debounce 500ms after the last change to any audio track
  - Runs core.render.render_audio_bed into
    {tempdir}/videditor_bed_{project_uuid}.wav
  - Signals: bed_ready(path), bed_failed(msg), bed_invalidated(), bed_unavailable()
  - bed_unavailable() is emitted when NoAudioError is caught
  - Cancels any in-flight render on invalidate. Never let two beds render at once.

  MainWindow owns one instance. Any command touching an audio track, and any project
  load, triggers it. Wire it now even though nothing consumes the bed until Phase 5.

ZOOM AND SCROLL:
  - Ctrl+wheel zooms around the mouse position, clamped 2 to 400 pixels_per_second
  - Plain wheel scrolls horizontally, shift+wheel vertically
  - Shift+Z fits the project to the window

BINDING: MainWindow holds the Project. TimelineScene.rebuild(project) clears and
re-creates all items. Crude on purpose. We optimise in Phase 4 only if it proves slow.

ACCEPTANCE:
  - Load a hand-built project with 3 video clips and 1 audio clip
  - Lanes, ruler, and clips render at correct positions and widths
  - Thumbnails and waveforms appear progressively, window never freezes
  - Zoom keeps the point under the cursor stationary
  - Editing the project JSON on disk and reloading triggers a bed render you can watch
    land in the temp directory

STOP.
```

---

## Phase 4: Editing operations and undo/redo

### Prompt

```
Phase 4 of 6. Add editing. This is the largest phase.

FILES TO CREATE:
  core/commands.py
  ui/timeline/interaction.py
Modify: ui/timeline/clip_item.py, ui/timeline/timeline_scene.py, ui/main_window.py

=== core/commands.py ===
Command pattern. Pure Python, no Qt. All time arguments are int ticks.

  class Command(ABC):
      def do(self, project: Project) -> None
      def undo(self, project: Project) -> None
      label: str
      touches_audio: bool      # drives audio bed invalidation

  Concrete commands:
      SplitClip(track_id, clip_id, at_ticks)
      TrimClip(track_id, clip_id, new_src_in, new_src_out, new_timeline_start)
      MoveClip(clip_id, from_track_id, to_track_id, new_timeline_start)
      DeleteClip(track_id, clip_id)
      DuplicateClip(track_id, clip_id)          # inserts right after the original
      SetClipGain(track_id, clip_id, new_gain_db)
      SetTrackMuted(track_id, muted)
      AddClipFromMedia(track_id, src, src_in, src_out, timeline_start)
      AddTrack(kind, name) / RemoveTrack(track_id)

  AddTrack CONSTRAINT: v1 has no compositing. core.filtergraph raises RenderError when
  more than one video track carries clips. AddTrack("video", ...) must refuse when a
  video track already exists, raising a clear error the UI surfaces as a disabled menu
  item, never as a failure discovered at export time. Audio tracks are unlimited.
  Add a test asserting a second AddTrack("video") is rejected.

  Each command stores whatever it needs to reverse itself EXACTLY. DeleteClip keeps
  the full Clip. TrimClip keeps the three previous values. RemoveTrack keeps the whole
  Track and its index. Never recompute an inverse.

  All commands snap incoming tick values to frame boundaries at the top of do(). One
  helper, called once per command.

  class CommandStack:
      push(cmd, project) / undo(project) / redo(project)
      can_undo() / can_redo() / undo_label() / redo_label()
      No Qt. MainWindow wraps it and emits the signals.

=== ui/timeline/interaction.py ===

DRAG TO MOVE:
  - Press on a clip body starts a drag, translucent ghost at the candidate position
  - Snapping, in priority order, within 8 screen pixels: playhead, then any clip edge
    on any track, then 0. Hold Alt to disable.
  - Vertical drag moves between tracks of the SAME kind only. Video onto an audio lane
    shows a red ghost and is rejected.
  - Overlapping drops rejected, ghost turns red, drop is a no-op.
  - Push exactly ONE MoveClip on mouse release. Not one per mouse move event. If undo
    ever replays a drag frame by frame, this rule was broken.

TRIM HANDLES:
  - 6px hot zones at each edge, cursor becomes SizeHorCursor
  - Left edge changes src_in AND timeline_start together
  - Right edge changes src_out only
  - Clamp: never past source media bounds, never below one frame of duration, never
    overlapping a neighbour
  - One TrimClip on release

SELECTION:
  - Click selects, Ctrl+click toggles, rubber band on empty space box-selects
  - Selected clips get a 2px accent border

PLAYHEAD:
  - Click or drag on the ruler moves it, snapping to frames
  - Playhead position lives on the scene, not in the model

SHORTCUTS on MainWindow:
    Space              play/pause
    S                  split selected clips at the playhead
    Delete             delete selected
    Ctrl+D             duplicate selected
    Ctrl+Z / Ctrl+Y    undo / redo
    Left / Right       one frame
    Shift+Left/Right   one second
    Home / End         start / end
    M                  toggle mute on the selected clip's track

AUDIO BED WIRING: after any command with touches_audio True, call
audio_bed_worker.invalidate(). One line. Do not forget it.

EDIT MENU: Undo and Redo showing the command label, greyed out when unavailable.

REBUILD: call scene.rebuild(project) after every command. If this visibly lags at 50
clips, tell me BEFORE switching to targeted item updates.

ACCEPTANCE:
  - Import two clips, drag onto a video track, trim both, split one, duplicate it,
    move one across tracks, then undo the entire session back to empty
  - Export at any point matches what the timeline shows
  - No interaction can produce overlapping clips
  - Trimming an audio clip triggers exactly ONE bed re-render, not one per mouse move

STOP.
```

---

## Phase 5: Timeline playback with audio as master clock

This replaced the drift-correction design from revision 1. It is shorter and more reliable.

### Prompt

```
Phase 5 of 6. Make the preview play the TIMELINE.

Create: ui/playback_controller.py
Modify: ui/preview_panel.py, ui/main_window.py
Delete: ui/single_player_controller.py

=== THE DESIGN. Do not substitute an alternative. ===

Two principles:

  1. AUDIO IS THE CLOCK. The pre-rendered bed WAV plays as one continuous file in one
     QMediaPlayer. Its reported position IS the timeline position. Nothing corrects
     the audio. Video is corrected to the audio.

     Rationale, put this in the module docstring: humans detect audio discontinuity
     far more readily than a video frame held one beat too long. Correcting video
     against audio produces invisible errors. Correcting audio against video produces
     audible clicks and pitch artefacts.

  2. VIDEO IS DOUBLE BUFFERED. The next clip is loaded, seeked and paused on its
     in-frame while the current clip plays. The cut is a widget raise, not a load.

=== ui/playback_controller.py ===

  class PlaybackController(QObject)

    def __init__(self, project, video_stage: VideoStage, bed_player: QMediaPlayer)

    State:
      _bed_path: Path | None
      _bed_valid: bool
      _current_clip_id: str | None
      _preloaded_clip_id: str | None
      _fallback_timer: QElapsedTimer      # only when there is no audio at all
      _fallback_origin_ticks: int

    Signals:
      position_changed(int)               # ticks
      playback_finished()
      bed_state_changed(str)              # "ready", "rendering", "unavailable"

    Public:
      set_bed(path: Path | None)          # from audio_bed_worker.bed_ready
      invalidate_bed()                    # from audio_bed_worker.bed_invalidated
      play() / pause() / seek(ticks: int) / is_playing()

  CLOCK RESOLUTION, in this order:
    1. bed available and valid -> position = bed_player.position() converted to ticks
    2. no audio in the project at all -> position from QElapsedTimer since play()
    3. bed mid-render -> play on the elapsed timer, show "Rendering audio preview" in
       the status line, switch to the bed the moment it lands

    NEVER block playback waiting for a bed. Degrade to the timer.

  THE TICK (a QTimer at 33ms, driving VIDEO ONLY, never driving audio):

    1. position = clock resolution above
    2. if position >= project.duration: pause, emit playback_finished, clamp
    3. target = clip_at(video_track, position)
    4. if target is None:
         video_stage.show_black()
         _current_clip_id = None
       elif target.id != _current_clip_id:
         if target.id == _preloaded_clip_id:
             video_stage.present_standby()          # the fast path, about one frame
         else:
             hard load into the active player and seek     # the slow path
         _current_clip_id = target.id
         schedule the preload of the NEXT clip
       else:
         drift check, below
    5. emit position_changed(position)

  DRIFT CHECK (video only, audio is NEVER corrected):
    expected_ms = ticks_to_seconds(clip.src_in + position - clip.timeline_start) * 1000
    if abs(active_player.position() - expected_ms) > 250:
        active_player.setPosition(expected_ms)
    The threshold is deliberately loose. A 250ms video-only correction is one held
    frame. Tightening it causes visible thrash. Do not lower it.

  PRELOADING:
    On entering a clip, call next_clip_after(video_track, position). If one exists and
    is not already the standby, call video_stage.preload(next.src, in_ms).
    If the next clip shares the SAME source file (a split), still preload into standby.
    Two players on one file is fine.

  SEEKING:
    seek(ticks) sets bed_player.setPosition, then clears _current_clip_id and
    _preloaded_clip_id so the next tick performs a full resolve. Works during playback.

  GAIN: clip.gain_db is baked into the bed by FFmpeg. Do NOT apply it in the player.
  The volume slider is a monitoring level only and must never touch the model.

  MUTED TRACKS: baked into the bed. Muting triggers a bed re-render via Phase 4's hook.
  The controller does nothing special.

=== PREVIEW PANEL ===
  - Scrubber spans project.duration
  - Timecode shows timeline position via ticks_to_timecode
  - Status line, muted text: "" when ready, "Rendering audio preview" while the bed
    builds, "No audio in project" when unavailable

=== TIMELINE COUPLING ===
  - position_changed moves the timeline playhead
  - Dragging the playhead calls seek()
  - Guard the feedback loop with an explicit re-entrancy flag, not signal blocking

=== WHAT MUST NOT EXIST IN THIS FILE ===
  - No clip-by-clip audio playback
  - No second audio player
  - No audio drift correction of any kind
  - No fixed 33ms position increment. Position is READ from the clock, never
    accumulated.

ACCEPTANCE, measure and report each:
  - 4-clip timeline with a music bed plays through every cut with no audible gap
  - Measure video-to-audio offset at the 60 second mark. Report the number.
  - Cut transitions: report whether any visible hitch remains, and roughly how long
  - Scrubbing during playback lands in the correct clip
  - Muting the audio track re-renders the bed and playback continues silently
  - A project with no audio track still plays via the fallback timer

STOP and report the two measurements above.
```

---

## Phase 6: Persistence, polish, packaging

### Prompt

```
Phase 6 of 6. Ship it.

PROJECT LIFECYCLE:
  - Dirty flag set by any command push, cleared on save
  - Prompt to save on close, New, and Open when dirty
  - Recent files, last 8, in QSettings
  - Window geometry, splitter sizes, zoom level in QSettings
  - Autosave to {project_path}.autosave every 120s when dirty. On startup, if an
    autosave is newer than its project file, offer recovery.

TEMP FILE HYGIENE:
  - Audio beds accumulate in temp. On clean exit, delete this project's bed.
  - On startup, delete any videditor_bed_*.wav older than 24 hours.

MISSING MEDIA:
  - On load, check every referenced source exists
  - Relink dialog listing missing files, letting the user pick a replacement file or a
    folder to search recursively by filename
  - Unresolved clips render with a hatched red pattern, are skipped on export with a
    warning, and never raise

EXPORT DIALOG (replacing Phase 2's bare save dialog):
  - Resolution preset (source, 1080p, 720p, 480p)
  - Quality mapping to CRF (High 18, Medium 20, Small 24)
  - Estimated output duration from project.duration
  - Progress with elapsed and estimated remaining
  - Cancel that terminates ffmpeg and deletes the partial file

ERROR SURFACE:
  - One show_error(parent, title, message, detail) helper
  - RenderError and ProbeError surface their ffmpeg stderr in an expandable Details
    section. Never a bare traceback dialog.

PACKAGING: build.spec for PyInstaller.
  - onedir, NOT onefile. onefile extracts to temp on every launch and it is slow.
  - Bundle bin/ffmpeg.exe and bin/ffprobe.exe as datas. core/binaries.py already
    handles sys._MEIPASS.
  - Exclude: QtWebEngine, QtQuick, QtQml, QtCharts, QtNetworkAuth, QtBluetooth,
    QtPositioning, QtTest, QtSql
  - Add an icon

  build.bat:
    .venv\Scripts\activate && pyinstaller build.spec --noconfirm

README.md covering: what it does, screenshot placeholder, the core/ui separation and
why it exists, the 120000-tick timebase and why not 90000, the edit model JSON schema,
how the filter graph compiler works, the audio-as-master-clock playback design, known
limitations, and build instructions.

ACCEPTANCE:
  - dist\videditor\videditor.exe runs on a machine with no Python installed
  - Full workflow from that build: import, edit, save, close, reopen, export
  - Bundle under 400MB

STOP. Report the final bundle size and any compromises.
```

---

## Revised schedule

| Phase | Evenings | Change from rev 1 |
|---|---|---|
| 0 | 3 | +1, the timebase module |
| 1 | 5 to 7 | +1, the audio bed renderer |
| 2 | 4 | +1, VideoStage built up front |
| 3 | 6 | +1, the bed worker |
| 4 | 6 to 8 | unchanged |
| 5 | 4 | **-1, this got simpler** |
| 6 | 4 | unchanged |

Roughly 6 to 8 weeks of evenings. About one week more than revision 1, in exchange for removing all three known defects.

---

## Where it will still go wrong

1. **Phase 1 filter graph.** `concat` demands identical parameters across all inputs. The error messages when it refuses are unhelpful. Test the generated string against short lavfi clips before pointing it at real media.

2. **Phase 3 bed debouncing.** If dragging an audio clip kicks off a render on every mouse move, the debounce or the invalidate hook is wrong. You will notice, because the fan spins up.

3. **Phase 5 preload timing.** If cuts still hitch, the preload is arriving too late or `standby_ready` is not firing. Log the interval between preload issue and standby ready. If it exceeds the remaining duration of the current clip, preload earlier, on clip entry rather than partway through.

4. **The temptation to skip Phase 1.** Getting UI on screen sooner feels like progress. It is the single most common way this project dies.

---

## Optional Phase 7: PyAV frame server

Only if the app is otherwise finished and you want to push it. This replaces `QMediaPlayer` entirely with your own decode path: a decoder thread per source, a ring buffer of decoded frames, `QImage` blitting in a custom `paintEvent`, and audio through `QAudioSink` fed by your own mixer.

Roughly two weeks. It buys true frame-accurate scrubbing with zero hitch anywhere, live transitions and effects in the preview, compositing and picture-in-picture, and scrub audio.

It also changes what the project is. "A desktop NLE with an undoable edit model and an FFmpeg filter graph compiler" becomes "a video editor with a custom frame server, ring-buffered decoder, and audio-clocked playback." The second is the version an interviewer digs into.

Do not start it until Phase 6 is done and the app is packaged.
