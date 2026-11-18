"""`make ext`: the real Chrome extension, unpacked in Playwright's Chromium, driven op by op.

It installs the extension in a temporary HOME, serves a test page with tests/bench/server.py,
starts Chromium with the extension loaded and its host registered in that profile, and runs
every op of src/docs/extension.md through the host. It needs `playwright install chromium`
and is never part of `make check`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PAGE = """<!doctype html>
<html><head><title>flnd test page</title></head>
<body style="height: 4000px">
<p id="marker">flnd-ext-marker</p>
<button id="go" onclick="document.getElementById('marker').textContent = 'clicked'">go</button>
<video src="clip.mp4"></video>
<img src="photo.png">
<script>
  document.cookie = "flnd_ext=1; path=/";
  localStorage.setItem("flnd_ext", "kept");
</script>
</body></html>
"""


def png(size: int) -> bytes:
    """A grey square PNG, `size` pixels each way."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return len(data).to_bytes(4, "big") + body + zlib.crc32(body).to_bytes(4, "big")

    rows = b"".join(b"\x00" + b"\x80" * size for _ in range(size))
    head = size.to_bytes(4, "big") * 2 + b"\x08\x00\x00\x00\x00"
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head) + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


def browsers() -> Path:
    """Where Playwright keeps its browsers, under the real HOME."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform == "win32":
        return Path.home() / "AppData" / "Local" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def site(folder: Path) -> None:
    folder.mkdir(parents=True)
    (folder / "index.html").write_text(PAGE, encoding="utf-8")
    (folder / "api.json").write_text(json.dumps({"flnd": "captured"}), encoding="utf-8")
    (folder / "photo.png").write_bytes(png(300))
    (folder / "clip.mp4").write_bytes(b"\x00" * 1024)


async def checks(base: str, worker: Any) -> list[tuple[str, bool, str]]:
    """Run every op once through the host, and say how each went; `worker` is the extension's
    service worker, as Playwright sees it."""
    from flnd.engine import extension

    client = None
    for _ in range(60):  # the extension starts its host as soon as Chromium loads it
        try:
            client = await extension.connect()
            break
        except Exception:
            await asyncio.sleep(0.5)
    if client is None:
        return [("hello", False, "the extension never connected")]
    found: list[tuple[str, bool, str]] = [("hello", client.version == "1.0.0", client.version)]

    async def check(op: str, test: Any, **args: Any) -> Any:
        try:
            value = await client.call(op, **args)
        except Exception as error:
            found.append((op, False, f"{type(error).__name__}: {error}"))
            return None
        try:
            ok = bool(test(value))
        except Exception as error:
            ok, value = False, f"{type(error).__name__}: {error}"
        found.append((op, ok, str(value)[:80]))
        return value

    async with client:
        opened = await check("tabs.open", lambda v: v["status"] == 200, url=f"{base}/index.html")
        if not opened:
            return found
        tab = opened["tab"]
        await check("tabs.list", lambda v: any(t["tab"] == tab for t in v))
        await check("document", lambda v: "flnd-ext-marker" in v["html"], tab=tab)
        await check("click", lambda v: v is None, tab=tab, css="#go")
        await check("scroll", lambda v: v["height"] >= 4000, tab=tab, by=500)
        await check("evaluate", lambda v: v["value"] == 3, tab=tab, script="1 + 2")
        # An answer over 768 KiB, which the extension sends in parts.
        await check("evaluate", lambda v: len(v["value"]) == 3 << 20, tab=tab,
                    script="'x'.repeat(3 << 20)")
        await check("cookies", lambda v: any(c["name"] == "flnd_ext" for c in v),
                    domains=["localhost"])
        await check("storage", lambda v: v.get("flnd_ext") == "kept", origin=base)
        capture = await check("capture.start", lambda v: "capture" in v, tab=tab,
                              match=r"api\.json")
        if capture:
            await client.call("evaluate", tab=tab,
                              script="fetch('/api.json').then((r) => r.text())")
            await check("capture.read", lambda v: any(
                json.loads(base64.b64decode(b["body"])) == {"flnd": "captured"} for b in v),
                capture=capture["capture"])
            await check("capture.stop", lambda v: v is None, capture=capture["capture"])
        await check("screenshot", lambda v: base64.b64decode(v["png"])[:4] == b"\x89PNG", tab=tab)
        # Ops that each attach the debugger, started at once on one tab, share one attach.
        alike = await asyncio.gather(
            *[client.call("evaluate", tab=tab, script=str(n)) for n in range(4)],
            client.call("screenshot", tab=tab), return_exceptions=True)
        failed = [f"{type(v).__name__}: {v}" for v in alike if isinstance(v, BaseException)]
        found.append(("4 evaluate + screenshot at once",
                      not failed and [v["value"] for v in alike[:4]] == [0, 1, 2, 3],
                      "; ".join(failed) or "all answered"))
        await check("media", lambda v: {"video", "image"} <= {m["kind"] for m in v}, tab=tab)
        await check("tabs.close", lambda v: v is None, tab=tab)
    # The token stays outside the unpacked folder, so the extension's origin can't fetch it.
    status = await worker.evaluate(
        "fetch(chrome.runtime.getURL('token.json')).then((r) => r.status, () => 'none')")
    found.append(("no token.json in the extension", status == "none", str(status)))
    return found


def install(temp: Path) -> None:
    """Install the extension, with its host registered in the test profile alone."""
    from flnd.engine import extension

    extension.install(home=temp / "home", profiles=[temp / "profile"], wait=0)
    # The test page's origin, which no plugin reads, is allowed in this copy alone.
    manifest = extension.unpacked() / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["host_permissions"].append("*://*.localhost/*")
    manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")


async def drive(temp: Path, port: int) -> list[tuple[str, bool, str]]:
    from playwright.async_api import async_playwright

    from flnd.engine import extension

    unpacked = str(extension.unpacked())
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(temp / "profile"), channel="chromium", headless=True, env=dict(os.environ),
            args=[f"--disable-extensions-except={unpacked}", f"--load-extension={unpacked}"])
        try:
            worker = (context.service_workers or [None])[0]
            worker = worker or await context.wait_for_event("serviceworker")
            return await checks(f"http://localhost:{port}", worker)
        finally:
            await context.close()


def main() -> int:
    from flnd.ui import out

    temp = Path(tempfile.mkdtemp(prefix="flnd-ext"))
    # Playwright finds its browsers under HOME, which moves next.
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers()))
    # Everything the extension and its host touch is in the temporary folder.
    os.environ.update({"HOME": str(temp), "XDG_RUNTIME_DIR": str(temp / "run"),
                       "FLND_TEST_NO_PROFILES": "1"})
    site(temp / "site")
    server = subprocess.Popen([sys.executable, str(ROOT / "tests" / "bench" / "server.py"),
                               str(temp / "site")], stdout=subprocess.PIPE, text=True)
    try:
        port = int(server.stdout.readline())  # type: ignore[union-attr]
        install(temp)
        found = asyncio.run(drive(temp, port))
    finally:
        server.terminate()
        server.wait()
        shutil.rmtree(temp, ignore_errors=True)
    for op, ok, said in found:
        out.line(("ok    " if ok else "FAIL  ", "good" if ok else "bad"), (op, "bold"),
                 ("  " + said, "dim"))
    return 0 if found and all(ok for _, ok, _ in found) else 1


if __name__ == "__main__":
    sys.exit(main())
