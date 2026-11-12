"""What a source may import besides `flnd.source`, `shared` and the stdlib.

Helpers more than one source shares: `dash` picks a DASH manifest's best streams, `relay` reads
the GraphQL results Meta embeds in its pages (`meta_results` loads one and reads them), `watch`
turns a page's JSON responses into an iterator (`watch_responses`), scrolling for more, and
`replay` sends a page's own pagination request again from the page with another cursor (`Replay`).
Sources import the modules themselves; nothing is re-exported here.

    from flnd.sources.common import dash
"""
