"""What a source may import besides `flnd.source`, `shared` and the stdlib.

Helpers more than one source shares: `dash` picks a DASH manifest's best streams, `relay` reads
the GraphQL results Meta embeds in its pages (`meta_results` loads one and reads them), and
`watch` turns a page's JSON responses into an iterator (`watch_responses`), scrolling for more.
Sources import the modules themselves; nothing is re-exported here.

    from flnd.sources.common import dash
"""
