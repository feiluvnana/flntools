"""The engine side's helpers: logins and secrets, and where files go.

The engine, the site and the job import these modules by name; nothing is re-exported here.
Sources and the CLI don't import them: a source talks to `flnd.source`, and the CLI to `flnd`.

    from flnd.common import paths
"""
