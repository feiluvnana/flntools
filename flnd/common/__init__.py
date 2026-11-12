"""The engine side's helpers: logins and secrets, compression, file writing, HTTP and pacing.

The engine, sync and the CLI import these modules by name; nothing is re-exported here. Sources
don't import them: a source talks to `flnd.source`.

    from flnd.common import net
"""
