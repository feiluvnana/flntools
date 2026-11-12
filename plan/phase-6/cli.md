# cli

Phase 6, step 3, with [session](session.md). flnd has five commands and 23 options, many of them pairs that only make sense together (`--compress-image` with `--format-image`) or that most runs never need. The user decided the shape below on 2026-11-12: four commands, and `download` with six options (`--classify` added later the same day).

## Commands

```
flnd download <targets…> [--from …] [--time …] [--image …] [--video …] [--audio …] [--classify]
flnd sync     [targets…]
flnd login
flnd logout   <sources…> | all
```

| Today | Phase 6 |
| --- | --- |
| `flnd download` with 11 options | `flnd download` with 6 ([below](#download)) |
| `flnd sync`, with `--only` and every download option | **`flnd sync [targets…]`**, with **no options**: repair, and fetch what's new ([below](#sync)) |
| `flnd login <source> [--method] [--path] [--browser] [--profile]` | **`flnd login`**, no arguments: every answer is asked ([below](#login)) |
| `flnd logout [source] [--all]` | **`flnd logout <sources…>`**, or `flnd logout all` |
| `flnd install-browser` | **removed**: flnd uses Google Chrome, and offers to download Chromium only when Chrome is missing ([below](#chrome-only)) |

## download

| Option | Phase 6 |
| --- | --- |
| `--from <categories>` | — |
| `--time <since>,<until>` | **new**: replaces `--since` and `--until` ([below](#--time)) |
| `--image <format>,<level>` | **new**: replaces `--compress-image` and `--format-image` |
| `--video <format>,<level>` | **new**: replaces `--compress-video` and `--format-video` |
| `--audio <format>,<level>` | **new**: the same for audio files, such as tlg's music and voice messages |
| `--classify` | **new**, `download` only: group each photo and video by category in `labels/<label>/` link folders, without renaming anything; saved for the target like `--image` ([classify](classify.md#flnd)) |
| `--unpack`, `--zip` | **removed** |
| `--locale`, `--timezone` | **removed**: Chrome uses the computer's own, which is what matches the connection |

Targets are still read from stdin, one per line, when none are given.

### `--time`

`--time <since>,<until>` keeps items dated in that range, both days included. Each side is a day (`YYYY-MM-DD`), or `*` or nothing for no limit:

```
--time 2026-01-01,2026-06-30     # the first half of 2026
--time 2026-01-01,'*'            # since New Year; the same as --time 2026-01-01,
--time ,2026-06-30               # up to the end of June
```

- **`*` must be quoted** in a shell (`'*'`), or zsh, macOS's default shell, stops with "no matches found" before flnd starts. The empty side needs no quoting, so README's examples use it, and help shows both.
- A value without a comma, an unreadable day, or a since after the until is a usage error: `--time takes <since>,<until>, such as 2026-01-01,2026-06-30; use * or leave a side empty for no limit`.
- Inside flnd nothing changes: `Options.since` and `Options.until`, and `session.since`, `session.until` and `session.wants(date=)`, are the same. A saved target's settings keep both fields.

### `--image`, `--video`, `--audio`

One option per kind of file, with a format, a level, or both, in either order:

```
--image webp,best        # every new photo as WebP, at the best level
--image mid              # the same format, smaller
--image avif             # AVIF, at the default level, high
--video mkv:h265,low     # a container and a codec, then the level
--video h265             # H.265 in the file's own container when it can hold it
--audio opus,mid
```

| Level | Means | media preset |
| --- | --- | --- |
| `best` | no visible loss: quality 95, full resolution, metadata stripped | new |
| `high` | quality 90, full resolution | `high` today |
| `mid` | quality 80, photos up to 2,304 px, videos up to 1080p | `balanced` today |
| `low` | quality 70, photos up to 1,536 px, videos up to 720p, 30 fps | `small` today |

- **One vocabulary.** `media.compress(preset=)` takes the same four names, so the CLI, the history and `media` agree. `small`, `balanced` and `high` go, and a history row that says `small` reads as `low`.
- **A format alone** uses `high`, because changing the format already re-encodes. A level alone keeps each file's format.
- **The formats** are those `media` writes. For `--image`: `jpg`, `webp`, `avif`, `png`, `gif`, `tif`, `bmp`. For `--video`, as `--format-video` today: a container, a codec (`h264`, `h265`, `vp9`), or `container:codec`. For `--audio`: `mp3`, `m4a`, `aac`, `opus`, `ogg`, `flac`.
- The file is still **replaced** only when the result is smaller, as in Phase 5.
- A bad value is a usage error that lists what each part takes: `--image webp,huge: the level is best, high, mid or low`.
- **Audio files** become a third kind of saved file, next to photos and videos: `.mp3`, `.m4a`, `.aac`, `.opus`, `.ogg`, `.oga`, `.flac` and `.wav`. Their extension also comes from their content, as for photos and videos.

### `--unpack` and `--zip` go

They were flnd's only use of `shared.archive`. `archive` itself stays in `shared` as a general tool ([api](api.md#archive-432-lines)), but `pyzipper` and `py7zr` leave flnd's dependencies.
- `session.save(…, password=)` goes, because it was only kept for `--unpack`. loi's unzip password is still written into the post's `info.md`.
- A target's saved settings lose `unpack` and `zip`; an old row that has them is read without them.
- Archives are saved as the site sends them. Unpack them with any archive tool.

## Saved settings

**`--image`, `--video`, `--audio` and `--classify` are saved for each target**, in `.flnd/targets.db`, and used by every later `download` and `sync` of it. They change only when a later `download` gives that option again, and that `download` applies the change to the files already saved as well.

```
flnd download ins:example_user --image webp,mid --classify    # saved for ins:example_user
flnd download ins:example_user                                # webp,mid and --classify again
flnd download ins:example_user --image best                   # --image is now best; --classify stays
flnd download ins:example_user --image off --classify off     # both turned off, and saved so
```

- **Each option is saved on its own**, so giving `--image` keeps the saved `--video`.
- **`off`** turns a saved option off: `--image off`, `--video off`, `--audio off`, `--classify off`. `--classify` alone means on.
- **`--from` and `--time` are not saved**, so a run's range never sticks to the next one by surprise. A target's first download, and a `sync`, take each source's default categories when `--from` isn't given.
- **Where they come from** is shown at the start of a run that uses saved ones: `ins:example_user: --image webp,mid --classify (saved)`.
- **Phase 5's `targets.db` rows** are read into the new form: `compress_image: "balanced"` with `format_image: "webp"` becomes `--image webp,mid`, and `unpack`, `zip`, `since` and `until` are dropped.

## sync

`flnd sync [targets…]` brings what's already downloaded up to date, and **changes nothing about how a target is downloaded**. Without targets it does every target in the history. It has no options. For each target, in order:
1. **repair:** files the history names that are missing, empty or broken are downloaded again; leftover `.part` files are removed; modified times are set from the recorded dates ([dates](dates.md));
2. **fetch:** posts that are new since the last run, with the target's [saved settings](#saved-settings) and each source's default categories;
3. a one-line **summary** per target, then the totals.

- **No options** (the user decided on 2026-11-12): `sync` never changes a target's settings, and never takes `--from` or `--time`. To change how a target is downloaded, run `flnd download` with the option. That download saves it.
- **`--only` goes**, and so does `sync`'s "compress what isn't compressed yet" step. Changing a setting is `download`'s job: a `download` that changes a target's `--image`, `--video`, `--audio` or `--classify` applies the new setting to that target's files already on disk too, then to the new ones, so a folder is never half one setting and half another.
- `flnd/sync.py` keeps its name, and `.flnd/targets.db` its rows.
- It exits 1 when any target failed ([fixes](fixes.md#exit-status)).

## login

`flnd login` takes no arguments and asks for everything, in this order:
1. **The source**, from a list: `Instagram (ins)`, `TikTok (tik)`, …. One already logged in says so, such as `Instagram (ins) · logged in`.
2. **The method**, when the source has more than one: `browser` or `file` for cookie sources, and `browser`, `qr` or `phone` for tlg.
3. **What the method needs:** the browser to read cookies from and its profile (the default profile is preselected), or the cookie file's path, or tlg's own questions.

- Without a terminal, `flnd login` exits 2 with `flnd login asks its questions in a terminal`. There's no flag form, because logging in is a one-time step done by a person.
- `--method`, `--path`, `--browser` and `--profile` go.

## logout

`flnd logout ins tik` removes those sources' logins, and `flnd logout all` removes every one. With no argument it is a usage error that lists the sources that have a login. A source without a login is reported (`tik: not logged in`), not an error.

## Chrome only

flnd drives **Google Chrome**, installed the usual way, and no longer installs Playwright's Chromium itself. This is built into `shared.browser`, so every caller gets it:
- **`browser.connect`** launches Chrome through Playwright's `chrome` channel.
- **When Chrome isn't installed**, it asks once, in a terminal: `Google Chrome isn't installed. Download Chromium for flnd (about 200 MB)? [y/N]`, with `prompt.confirm`.
  - On yes, it downloads Chromium with Playwright's own driver (`install --no-shell chromium`), under a `log.progress` row, then launches it. Later runs find it and use it without asking.
  - On no, or without a terminal, it raises `RuntimeError("flnd needs Google Chrome: install it from https://www.google.com/chrome/")`.
- **This is the one question `flnd download` can ask.** It comes before any work starts, inside `log`'s question handling, so the live rows are held while it's open.
- `setup` downloads no browser at all ([setup](setup.md#design)), and `flnd install-browser` goes.
- `login`'s "read cookies from" list still offers Chrome, Edge, Brave, Firefox and the Chromium browser. Those read a browser's cookie store, not the browser flnd drives, so nothing changes there.

## What changes in the code

- `flnd/cli.py`: four commands. Its docstring's command list, and the `Preset`/`SyncPreset` types, become one `MediaChoice` parser.
- `Options(categories, since, until, image, video, audio)`, where `image`, `video` and `audio` are each `Choice(format, level) | None`. `unpack`, `zip` and the four `compress_*` / `format_*` fields go.
- `Engine(*, home, sources)`: `limit`, `headless`, `locale` and `timezone` go ([simplify](simplify.md)). `browser.connect` keeps its keywords; flnd stops passing `locale=` and `timezone=`.
- `flnd/compress.py` ([session](session.md#what-moves-out-of-sessionpy)) works on the three kinds through one table: kind → its option, its formats, and whether it waits for the one-at-a-time encode lock (videos only).

## Tests

- `tests/test_cli.py`:
  - `--image webp,best`, `--image best,webp`, `--image mid` and `--video mkv:h265,low` parse to the right `Choice`;
  - `--image webp,huge` and `--video webm:h265` are usage errors naming what's allowed;
  - `--time 2026-01-01,`, `--time '*',2026-06-30` and `--time 2026-01-01,2026-06-30` give the right `since` and `until`; `--time 2026-01-01`, `--time 2026-13-01,` and a since after the until are usage errors;
  - `--unpack`, `--zip`, `--locale`, `--since`, `--compress-image` and `flnd sync` are unknown.
- `flnd login` with a fake screen answers the source, method and browser questions in order. Without a terminal it exits 2.
- `flnd logout all` removes every stored login; `flnd logout` alone is a usage error that lists the logged-in sources.
- `browser.connect` with no Chrome (a patched channel lookup):
  - asks once in a terminal;
  - downloads and launches on yes;
  - raises naming Chrome on no, or without a terminal.
- An old `targets.db` row with `unpack`, `zip` and `compress_image: "small"` reads as `image=Choice(None, "low")`.
