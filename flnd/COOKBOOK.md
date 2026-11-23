# flnd cookbook

Common uses, each a goal and the command that does it. Every recipe links to the part of
[SPEC.md](SPEC.md) that says everything about it: grammars, refused forms and exact errors. The
examples use `example_user` and `example_handle`; put a real name in its place. Quote a target
that has a `|`, since `|` is the shell's pipe.

## Download a profile

Goal: save everything a user has posted.

```sh
flnd get ins:example_user
```

The plugin's code, a colon, the user. Posts, reels, stories and the rest of what the account
owns are saved under `media/ins/example_user/`. The same command with another code does
Twitter/X (`twt:example_handle`), TikTok (`tik:example_creator`), Threads (`thr:`), Facebook
(`fbk:`), and so on. A profile link works too: `flnd get https://www.instagram.com/example_user/`.
More: [Targets](SPEC.md#targets), [Folders](SPEC.md#folders).

## Pick categories

Goal: take only some parts of a profile, or the parts that are not taken by default.

```sh
flnd get "ins:example_user|posts,reels"
flnd get "ins:example_user|tagged"
flnd get "ins:example_user|all"
```

After `|`, a comma list of categories. Opt-in ones (such as `tagged`) need their name; `all`
takes every category.
More: [Categories](SPEC.md#categories).

## A single post

Goal: save one post, not the whole profile.

```sh
flnd get https://www.instagram.com/p/C1abc/
flnd get ins:post/C1abc
```

A post link, or `<plugin>:<kind>/<id>`. It goes in its owner's folder.
More: [A single post](SPEC.md#a-single-post).

## A search

Goal: save what a search finds. A search needs `--folder`, which says where all its hits go.

```sh
flnd get "twt?example words" --folder="{query}"
```

More: [A search needs a folder](SPEC.md#a-search-needs-a-folder),
[Searches and `--limit`](SPEC.md#searches-and---limit).

## Choose the folder

Goal: put the files where you want them, one folder for the target or one per category.

```sh
flnd get twt:example_handle --folder="example_handle"
flnd get "ins:example_user|all" --folder="posts:{target}/feed,*:{target}/other"
```

A template of fields such as `{target}`, `{category}` and `{date:%Y-%m}`. A list gives each
category its own, and `*` is every category the list does not name.
More: [`--folder`](SPEC.md#--folder), [One folder per category](SPEC.md#one-folder-per-category).

## Follow and sync

Goal: keep a profile up to date.

```sh
flnd get ins:example_user
flnd get
```

A profile you get is followed. `flnd get` alone then gets what is new for every followed target,
and stops at the posts it already has. `flnd target list` shows them, and `f` there unfollows.
More: [Sync](SPEC.md#sync).

## A date window

Goal: only the posts of some days.

```sh
flnd get ins:example_user --when 2026-01..2026-03
flnd get ins:example_user --when 30d
```

A year (`2026`), a month, a day, a range (`2026-01..2026-03`, `2026-01..`, `..2026-03`), or a
span back from today (`30d`, `8w`, `1y`). `--when ""` is every day.
More: [Dates](SPEC.md#dates).

## A dry run

Goal: see what would be saved, and save nothing.

```sh
flnd get ins:example_user --dry
flnd get ins:example_user --dry --limit 5
```

It prints `new <path>` for a file it would save and `saved <path>` for one you have.
More: [Sync](SPEC.md#sync).

## Log in and switch accounts

Goal: use your login for a site, and use a second account when the first is blocked.

```sh
flnd auth login ins chrome
flnd auth login ins file cookies.txt
flnd auth list
```

A method is `chrome` (your Chrome, through the extension), `browser`, `file <path>`, `firefox`,
`safari`, or, for Telegram, `qr` and `phone`. Log in again as another account to keep both.
`flnd auth list` shows them, and `d` on a terminal logs one out. To let a request the first
account cannot make move on to the next, set `switch = true` under `[defaults]` in
`config.toml` (`flnd config` edits it).
More: [Logins and accounts](SPEC.md#logins-and-accounts), [Config](SPEC.md#config).

## Run a program on every file

Goal: do something with each saved file, such as shrink a video with ffmpeg.

```sh
flnd get ins:example_user --exec 'ffmpeg -n -i {path} -vf scale=-2:720 {path}.720.mp4'
```

`{path}`, `{target}` and `{post}` fill in each file's values, and the program runs once per
file with no shell. A program that fails on a file is a warning, and the run goes on.
More: [Sync](SPEC.md#sync).

The run screen's `use` line counts flnd's processes (itself, the browser, your `--exec` program):
how many, their memory and their CPU %, then the browser alone.

## Fix a failed run

Goal: a run said some files failed, or the disk and the history disagree.

```sh
flnd status
flnd get ins:example_user --again
```

`flnd status` shows the last run's failures and compares the history with the disk. On a
terminal it offers to repair the rows whose file is gone or has the wrong size. `--again` lists
every post again and fetches what is missing. A failed file is also retried on the next runs of
its target.

More: [check and repair](SPEC.md#check-and-repair),
[When it goes wrong](SPEC.md#when-it-goes-wrong), [Sync](SPEC.md#sync).
