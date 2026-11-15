"""The engine side's helpers: logins and secrets, file writing, HTTP and pacing.

The engine, the site and the job import these modules by name; nothing is re-exported here.
Sources and the CLI don't import them: a source talks to `flnd.source`, and the CLI to `flnd`.

    from flnd.common import net
"""
