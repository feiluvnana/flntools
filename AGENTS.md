# Agent guidelines

These rules are for anyone, person or agent, who changes this repository. It holds `flnd/`
(the downloader), `flnh/` (the folder/file hider), and the shared `flnkit/` package. Each project
keeps its own rules in `<project>/AGENTS.md`, and runs its commands inside its own folder.

## Commits
- Every message has the form `<action>:<summary>`.
  - `<action>` is one of `feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `chore`, `style`.
  - `<summary>` is lowercase and imperative, with no period at the end.
  - Example: `fix:flush a download part before saving its offsets`.
- No co-author trailer, and no other attribution line.
- Commit as feiluvnana, signed with SSH, through the repo-local git config. Never change the
  global config.
- One logical change per commit.
- Run `git pull --rebase` first. Commit only your own paths (`git commit -- <paths>`), because
  other sessions may share the working tree.
- Push only when the user says to.

## Privacy
- Never pass text from a site (a file name, a caption, any other text) to a shell. Run commands
  with an argument list.
- Never put a cookie value, key, token or password in a log, an error, a comment, a doc or a
  test.
- The run journal is the one log that keeps a download's full URL, query included; headers that
  carry secrets are still scrubbed.
- Live checks are done by hand, on the test accounts, and never in unit tests.
- Unit tests fake the network, the browser and the clock.

## Checks
- Ruff is the one checker, with no type checker. Type hints are for readers.

## Writing
- Lines are at most 100 characters.
- Use plain English.

## Models
- Mechanical work goes to the low-cost model (`model: "haiku"` for a subagent). Mechanical
  means moves, renames, call sites, snapshots, and doc edits that follow a decision.
- Design, new behaviour, debugging, security-sensitive code, reviews and new docs pages use the
  default model.
