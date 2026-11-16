"""What a source may import besides `flnd.source` and the stdlib: the private `flnd._x` toolkit.

Helpers more than one source shares: `dash` picks a DASH manifest's best streams, `relay` reads
the GraphQL results Meta embeds in its pages (`meta_results` loads one and reads them), and
`feed` starts loading the pages a browser source opens next in background tabs (`warm_tabs`).
Sources import the modules themselves; nothing is re-exported here.

    from flnd.sources.common import dash
"""
