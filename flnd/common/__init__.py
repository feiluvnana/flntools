"""The engine side's helpers: logins and secrets, compression, file writing, HTTP and pacing.

The engine, the session, sync and the CLI import these modules by name; nothing is re-exported
here. Once Phase 6's step 6 is done, sources don't import them: a source talks to `flnd.source`.
Until then, today's sources still import `flnd.session`.

    from flnd.common import net
"""
