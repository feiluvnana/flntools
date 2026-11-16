"""A loopback HTTP/1.1 file server for the benchmarks: keep-alive, `Range` and `sendfile`.

    python tests/bench/server.py FOLDER

`kit.serve` starts it in a child process. It binds a free port on 127.0.0.1 and prints that port
as its first line on stdout, then serves `FOLDER` until it is terminated.

How it answers:
1. it reads requests on each connection until the client closes it (keep-alive);
2. a path under `/slow/` waits 50 ms first, then answers the same file without the prefix: the
   browser group's synthetic feed uses it to make each batch arrive late, as a real feed does;
3. a `Range: bytes=a-b` header gets a 206 with that slice, so segmented downloads work;
4. the body is sent with `loop.sendfile`, so the server costs little CPU and the numbers measure
   the client, not this file.

It is deliberately small: no chunked encoding, no HEAD, no error pages beyond a bare 404.
"""

from __future__ import annotations

import asyncio
import os
import sys

# Content types by suffix: the browser needs text/html for the feed page and JSON for its batches.
TYPES = {".html": "text/html", ".json": "application/json"}
# How long a `/slow/` request waits before answering, in seconds.
SLOW = 0.05


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, root: str) -> None:
    """Answer every request on one connection, until the client closes it.

    Args:
        reader: the connection's incoming side.
        writer: the connection's outgoing side.
        root: the folder being served.
    """
    loop = asyncio.get_running_loop()
    try:
        while True:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
            path = head[0].split(" ")[1].split("?")[0]
            headers = {}
            for line in head[1:]:
                name, _, value = line.partition(":")
                headers[name.lower()] = value.strip()
            if path.startswith("/slow/"):
                await asyncio.sleep(SLOW)
                path = path.removeprefix("/slow")
            full = os.path.join(root, path.lstrip("/"))
            if not os.path.isfile(full):
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                continue
            size = os.path.getsize(full)
            start, end = 0, size - 1
            status, extra = "200 OK", "Accept-Ranges: bytes\r\n"
            if "range" in headers:
                first, _, last = headers["range"].removeprefix("bytes=").partition("-")
                start, end = int(first or 0), int(last) if last else size - 1
                status = "206 Partial Content"
                extra += f"Content-Range: bytes {start}-{end}/{size}\r\n"
            length = end - start + 1
            kind = TYPES.get(os.path.splitext(full)[1], "application/octet-stream")
            writer.write(f"HTTP/1.1 {status}\r\nContent-Length: {length}\r\n"
                         f'Content-Type: {kind}\r\nETag: "x"\r\n{extra}\r\n'.encode())
            with open(full, "rb") as body:
                await loop.sendfile(writer.transport, body, start, length)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        pass  # the client closed the connection: the normal end of a keep-alive session
    finally:
        writer.close()


async def main(root: str) -> None:
    """Bind a free loopback port, print it, and serve `root` until terminated.

    Args:
        root: the folder to serve.
    """
    server = await asyncio.start_server(lambda r, w: handle(r, w, root), "127.0.0.1", 0,
                                        backlog=512)
    print(server.sockets[0].getsockname()[1], flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
