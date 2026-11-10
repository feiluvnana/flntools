# Agent Guidelines

## Commit Convention

All commits must strictly follow this message format:

```
<action>:<summary>
```

### Format Rules
- `<action>`: Lowercase verb or type representing the action taken (e.g., `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `style`, `perf`).
- `:`: Colon immediately follows `<action>` without spaces before it.
- `<summary>`: Concise description of the changes made, written in imperative lowercase without a trailing period.
- **NO CO-AUTHOR**: Do not include `Co-authored-by:` trailers or any other co-author attributions.

### Examples
- `feat:implement continuous scroll to end for all crawlers`
- `fix:support user reposts extraction in instagram extractor`
- `fix:add user originals timeline posts crawling for twitter`
- `test:add unit tests for source parsing and scrolling logic`
- `chore:initialize git repository and github remote`
- `docs:update readme with posts and reposts folder layout`
