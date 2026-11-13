# shrink

Phase 7, step 2, **built** 2026-11-13. The user decided on 2026-11-13: **`--image`, `--video` and `--audio` go, and one
flag, `--shrink`, replaces them.** With it, flnd compresses each saved photo, video and audio file
to a balanced quality.

## The option

```
flnd download ins:example_user --shrink        # on, saved for the target
flnd download ins:example_user --shrink off    # off again
```

- **What it does:** each photo, video and audio file is compressed at `media`'s `mid` level, the
  balance of quality and size (Phase 5's "balanced"), **in its own format**: a JPEG stays a JPEG,
  an MP4 stays an MP4 in its own codec, so every file still opens where it opened before. The
  smaller file replaces the original only when it is smaller, metadata (GPS too) stripped, as
  today. Videos still encode one at a time across the run.
- **Saved for each target**, as `--image` was: in `targets.db`, used by every later `download`
  and `sync` of it, changed only by a `download` that gives `--shrink` or `--shrink off`. A
  download that turns it on shrinks the target's files already saved too, so a folder is never
  half shrunk.
- **No format or level to choose.** A person who wants WebP or H.265 converts afterwards; flnd
  picks the one setting that is safe for every file.
- `cli.opt(const=)` takes the flag form, as `--classify` did: `--shrink` alone means on.

## What goes

- `--image`, `--video`, `--audio`, and their `<format>,<level>` parsing: `flnd.Choice` and
  `Choice.parse`, `FORMAT_LEVEL`, the format and codec tables (`IMAGE_FORMATS`,
  `VIDEO_CONTAINERS`, `VIDEO_CODECS`, `AUDIO_FORMATS`, `CODECS_IN`), and the error messages for a
  bad format or level.
- **Phase 5 rows** (`compress_image: "balanced"`, `OLD_LEVELS`): with [layout](layout.md)'s new
  `metadata/` and no migration, no old `targets.db` is read, so nothing old needs reading.
- `Engine.download(image=, video=, audio=)` becomes `Engine.download(shrink=)`: True, False (off),
  or None (keep the saved one). A history row's `compressed` records `"mid"`.
- About 60 lines of `compress.py`, `settings.py` and `cli.py`, on top of [lean](lean.md); the API
  snapshot loses `flnd.Choice`.

## Docs

README's usage line, the options table and "saved settings" say `--shrink`. The run's start line
for saved settings says `ins:example_user: --shrink (saved)`.

## Tests

`--shrink` saves on and shrinks a target's saved files; `--shrink off` saves off and touches no
file; a later `sync` shrinks only new files; a file that comes out bigger keeps its original; a
JPEG stays JPEG and an MP4 stays MP4; `--image` is an unknown option.

## As built (2026-11-13)

- `media.compress` without a codec writes the container's default (H.264 in an MP4), so flnd
  passes the video's own codec when media can write it (`OWN_CODECS`: `h264`, `hevc` as `h265`,
  `vp9`); any other codec gets the container's default, which still opens everywhere the MP4 did.
  `shared.media` is unchanged.
- `targets.db` saves `shrink: true|false`. `Engine.download(shrink=)` is True, False or None, and
  the CLI's `--shrink [on|off]` maps onto it (`cli.opt(choices=("on", "off"), const="on")`).
- `apply` shrinks every photo, video and audio row whose `compressed` isn't `"mid"`; a `"none"` row
  (nothing smaller last time) is tried again when `--shrink` is turned on again, as before.
- lean E3 (`post.py` into the job) is not part of this step; it is its own commit before step 3.
