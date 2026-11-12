"""The engine side's helpers: logins and secrets, compression, file writing, HTTP and pacing.

The engine, its settings and repair, the site and the job import these modules by name; nothing
is re-exported here. Sources and the CLI don't import them: a source talks to `flnd.source`, and
the CLI to `flnd`.

    from flnd.common import net
"""
