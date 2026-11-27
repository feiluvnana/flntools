# flnd

flnd downloads photos, videos and files from the sites you follow, and keeps them up to date.
It is a command line, and new sites are added as built-in plugins.
Built in: Instagram (`ins`), TikTok (`tik`), Twitter/X (`twt`), Facebook (`fbk`), Threads
(`thr`), LOIBUS (`loi`), mimihentai (`mht`), SimpCity (`smp`), pixiv (`pxv`), and `web` for any
other link.

## Install

flnd needs [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer. Some files also need
Google Chrome, ffmpeg or 7-Zip on your PATH.

```sh
git clone https://github.com/feiluvnana/flntools
cd flntools
uv tool install -e ./flnd
```

## Use

```sh
flnd auth login ins cookies.txt        # log in to Instagram with a cookie file
flnd get ins:example_user         # its own content: posts, reels, stories, ...
flnd get                          # what is new for every target you have followed
```

Downloads and history live in `./.flnd` (or `$FLND_HOME`, or `--home DIR`); logins live in
`~/.config/flnd/logins/`.

## More

- [COOKBOOK.md](COOKBOOK.md): short recipes for the common uses.
- [SPEC.md](SPEC.md): everything, with no gray area: targets, dates, categories, folders, sync,
  logins, errors, every command and flag, every setting and every plugin.
- Rules for changing the code are in [AGENTS.md](AGENTS.md).
