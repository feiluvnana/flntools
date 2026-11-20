// The functions flnd's extension runs inside a page, through chrome.scripting
// (COOKBOOK.md#the-chrome-extension). Chrome copies each function's text into the page, so each one stands
// alone: nothing outside its own body is in scope there.

// The page's HTML, as it is now.
export function html() {
  return document.documentElement.outerHTML;
}

// The HTTP status of the page's own document; null when the browser doesn't say.
export function status() {
  const [navigation] = performance.getEntriesByType("navigation");
  return navigation?.responseStatus || null;
}

// Click the first element `css` matches, waiting up to `seconds` for one; false if none came.
export async function click(css, seconds) {
  const until = Date.now() + seconds * 1000;
  for (;;) {
    const found = document.querySelector(css);
    if (found) {
      found.scrollIntoView({ block: "center" });
      found.click();
      return true;
    }
    if (Date.now() >= until) return false;
    await new Promise((done) => setTimeout(done, 100));
  }
}

// Scroll down by `by` pixels (up when negative); the page's height after it.
export function scroll(by) {
  window.scrollBy(0, by);
  return document.documentElement.scrollHeight;
}

// The page's localStorage, as {key: value}.
export function storage() {
  const found = {};
  for (let index = 0; index < localStorage.length; index++) {
    const key = localStorage.key(index);
    found[key] = localStorage.getItem(key);
  }
  return found;
}

// The media the page shows or loaded: videos, HLS and DASH playlists, and large images.
// `size` is the bytes the browser fetched, when it knows; blob: and data: URLs are left out.
export function media() {
  const fetched = new Map();
  for (const entry of performance.getEntriesByType("resource")) {
    fetched.set(entry.name, entry.encodedBodySize || entry.transferSize || null);
  }
  const found = new Map();
  const kind = (url) => {
    const path = new URL(url).pathname.toLowerCase();
    if (path.endsWith(".m3u8")) return "hls";
    if (path.endsWith(".mpd")) return "dash";
    return null;
  };
  const add = (url, type) => {
    if (!url || url.startsWith("blob:") || url.startsWith("data:")) return;
    const whole = new URL(url, location.href).href;
    if (!found.has(whole)) {
      found.set(whole, { url: whole, kind: type, size: fetched.get(whole) ?? null });
    }
  };
  for (const url of fetched.keys()) {
    const type = kind(url);
    if (type) add(url, type);
  }
  for (const video of document.querySelectorAll("video")) {
    const inside = [...video.querySelectorAll("source")].map((source) => source.src);
    for (const url of [video.currentSrc, video.src, ...inside]) {
      if (!url || url.startsWith("blob:")) continue;
      add(url, kind(new URL(url, location.href).href) ?? "video");
    }
  }
  // An image smaller than this, in pixels each way, is an icon or an avatar, not a photo.
  const small = 200;
  for (const image of document.images) {
    if (image.naturalWidth >= small && image.naturalHeight >= small) {
      add(image.currentSrc || image.src, "image");
    }
  }
  return [...found.values()];
}
