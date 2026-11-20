// flnd's service worker: the port to `flnd host`, every op a run sends through it, and the
// popup's questions (COOKBOOK.md#the-chrome-extension).
import * as page from "./page.js";

const HOST = "com.flnd.host";
// Chrome caps a host's message at 1 MiB, so a message over LIMIT bytes goes in parts of PART
// bytes, whose base64 is LIMIT; the host does the same, and both put parts back together.
const LIMIT = 768 * 1024;
const PART = 576 * 1024;
// How long a tab may take to load, and the popup to wait for flnd, in milliseconds.
const LOAD = 60000;
const ANSWER = 30000;
// How long `click` waits for its element, in seconds.
const CLICK = 10;
// The hint when Chrome hasn't let the extension read a site.
const allow = (hosts) => `open the flnd popup to allow ${hosts}`;

let port = null;
let asked = 0;
let made = 0;
const questions = new Map(); // the popup's questions to flnd, by id
const joining = new Map(); // parts of a message, by id
// The open captures by id: {tab, pattern, seen: requestId -> {url, status}, bodies, pending}.
const captures = new Map();
// By tab: {reasons, ready}, why chrome.debugger is attached to it and the attach itself, kept
// before it is awaited so ops that start at once share one attach.
const attached = new Map();
const leaving = new Map(); // by tab: a detach under way, which the next attach waits for

// ------------------------------------------------------------------------------------------
// The port
// ------------------------------------------------------------------------------------------

function connect() {
  if (port) return port;
  port = chrome.runtime.connectNative(HOST);
  port.onMessage.addListener((message) => receive(message));
  port.onDisconnect.addListener(() => {
    port = null;
    for (const waiting of questions.values()) waiting.reject(new Error("flnd's host went away"));
    questions.clear();
    joining.clear();
  });
  return port;
}

function send(message) {
  const open = connect();
  for (const piece of split(message)) open.postMessage(piece);
}

async function receive(message) {
  if ("part" in message) {
    message = join(message);
    if (!message) return;
  }
  if ("op" in message) {
    send(await answer(message));
    return;
  }
  const waiting = questions.get(message.id);
  if (!waiting) return;
  questions.delete(message.id);
  if (message.ok) waiting.resolve(message.value);
  else waiting.reject(fail("Error", message.error?.message ?? "flnd failed", message.error?.hint));
}

async function answer({ id, op, args }) {
  const run = OPS[op];
  try {
    if (!run) throw fail("UsageError", `the extension has no op ${op}`);
    return { id, ok: true, value: (await run(args ?? {})) ?? null };
  } catch (error) {
    const said = { class: error.flnd ?? "Error", message: String(error.message ?? error) };
    return { id, ok: false, error: { ...said, hint: error.hint ?? "" } };
  }
}

function fail(cls, message, hint = "") {
  return Object.assign(new Error(message), { flnd: cls, hint });
}

// ------------------------------------------------------------------------------------------
// Parts: JSON in base64 pieces
// ------------------------------------------------------------------------------------------

function split(message) {
  const data = new TextEncoder().encode(JSON.stringify(message));
  if (data.length <= LIMIT) return [message];
  const count = Math.ceil(data.length / PART);
  const pieces = [];
  for (let index = 0; index < count; index++) {
    const chunk = base64(data.subarray(index * PART, (index + 1) * PART));
    pieces.push({ id: message.id, part: index, of: count, chunk });
  }
  return pieces;
}

function join({ id, part, of, chunk }) {
  const held = joining.get(id) ?? new Array(of).fill(null);
  joining.set(id, held);
  held[part] = bytes(chunk);
  if (held.some((piece) => piece === null)) return null;
  joining.delete(id);
  const whole = new Uint8Array(held.reduce((total, piece) => total + piece.length, 0));
  let at = 0;
  for (const piece of held) {
    whole.set(piece, at);
    at += piece.length;
  }
  return JSON.parse(new TextDecoder().decode(whole));
}

function base64(data) {
  let text = "";
  for (let at = 0; at < data.length; at += 0x8000) {
    text += String.fromCharCode(...data.subarray(at, at + 0x8000));
  }
  return btoa(text);
}

function bytes(text) {
  const raw = atob(text);
  const data = new Uint8Array(raw.length);
  for (let index = 0; index < raw.length; index++) data[index] = raw.charCodeAt(index);
  return data;
}

// ------------------------------------------------------------------------------------------
// Pages and tabs
// ------------------------------------------------------------------------------------------

// Run one function of page.js in `tab`, with `args`, and return what it returned.
async function inPage(tab, func, ...args) {
  let results;
  try {
    results = await chrome.scripting.executeScript({ target: { tabId: tab }, func, args });
  } catch (error) {
    const host = await chrome.tabs.get(tab).then((found) => new URL(found.url).hostname, () => "");
    const said = `flnd's extension can't read tab ${tab}: ${error.message}`;
    throw fail("UsageError", said, host ? allow(host) : "");
  }
  return results?.[0]?.result;
}

// Fail with the hint to allow a site when Chrome hasn't granted the extension its origin.
async function granted(url) {
  if (!URL.canParse(url)) return;
  const { protocol, hostname } = new URL(url);
  if (protocol !== "http:" && protocol !== "https:") return;
  if (await chrome.permissions.contains({ origins: [`${protocol}//${hostname}/*`] })) return;
  throw fail("UsageError", `flnd's extension may not read ${hostname}`, allow(hostname));
}

function loaded(tab) {
  return new Promise((resolve, reject) => {
    const late = () => done(fail("NetworkError", `the tab didn't load in ${LOAD / 1000} s`));
    const timer = setTimeout(late, LOAD);
    const updated = (id, change, found) => {
      if (id === tab && change.status === "complete") done(null, found);
    };
    const removed = (id) => {
      if (id === tab) done(fail("NetworkError", "the tab was closed while it loaded"));
    };
    function done(error, found) {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(updated);
      chrome.tabs.onRemoved.removeListener(removed);
      if (error) reject(error);
      else resolve(found);
    }
    chrome.tabs.onUpdated.addListener(updated);
    chrome.tabs.onRemoved.addListener(removed);
    chrome.tabs.get(tab).then((found) => {
      if (found.status === "complete" && !found.pendingUrl) done(null, found);
    }, () => {});
  });
}

// ------------------------------------------------------------------------------------------
// chrome.debugger: attached while a capture is open or a script runs, then detached
// ------------------------------------------------------------------------------------------

async function attach(tab, reason) {
  // chrome.debugger ignores host permissions, so the grant is checked here.
  await granted((await chrome.tabs.get(tab)).url ?? "");
  let held = attached.get(tab);
  if (!held) {
    const after = leaving.get(tab) ?? Promise.resolve();
    const ready = after.then(() => chrome.debugger.attach({ tabId: tab }, "1.3"));
    held = { reasons: new Set(), ready };
    attached.set(tab, held);
  }
  held.reasons.add(reason);
  try {
    await held.ready;
  } catch (error) {
    held.reasons.delete(reason);
    if (attached.get(tab) === held) attached.delete(tab); // the next op tries again
    const said = `flnd's extension can't watch tab ${tab}: ${error.message}`;
    throw fail("UsageError", said, "close DevTools on that tab");
  }
}

async function detach(tab, reason) {
  const held = attached.get(tab);
  if (!held?.reasons.delete(reason) || held.reasons.size) return;
  attached.delete(tab);
  const going = chrome.debugger.detach({ tabId: tab }).catch(() => {});
  leaving.set(tab, going);
  await going;
  if (leaving.get(tab) === going) leaving.delete(tab);
}

function command(tab, method, params = {}) {
  return chrome.debugger.sendCommand({ tabId: tab }, method, params);
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  for (const capture of captures.values()) {
    if (capture.tab !== source.tabId) continue;
    if (method === "Network.responseReceived" && capture.pattern.test(params.response.url)) {
      const { url, status } = params.response;
      capture.seen.set(params.requestId, { url, status });
    } else if (method === "Network.loadingFinished" && capture.seen.has(params.requestId)) {
      const { url, status } = capture.seen.get(params.requestId);
      capture.seen.delete(params.requestId);
      const { requestId } = params;
      const asked = command(capture.tab, "Network.getResponseBody", { requestId });
      const reading = asked.then(
        (found) => {
          const text = found.base64Encoded ? null : new TextEncoder().encode(found.body);
          capture.bodies.push({ url, status, body: text ? base64(text) : found.body });
        },
        () => {}, // the body is gone (the page navigated away): nothing to read
      );
      capture.pending.add(reading);
      reading.finally(() => capture.pending.delete(reading));
    }
  }
});

chrome.debugger.onDetach.addListener((source) => {
  attached.delete(source.tabId);
  for (const [id, capture] of captures) if (capture.tab === source.tabId) captures.delete(id);
});

chrome.tabs.onRemoved.addListener((tab) => {
  attached.delete(tab);
  for (const [id, capture] of captures) if (capture.tab === tab) captures.delete(id);
});

// ------------------------------------------------------------------------------------------
// The ops (COOKBOOK.md#the-chrome-extension lists their arguments and values)
// ------------------------------------------------------------------------------------------

const OPS = {
  hello: async () => ({ version: chrome.runtime.getManifest().version }),

  cookies: async ({ domains = [] }) => {
    const origins = domains.map((domain) => `*://*.${domain}/*`);
    if (!(await chrome.permissions.contains({ origins }))) {
      const said = `flnd's extension may not read the cookies of ${domains.join(", ")}`;
      throw fail("UsageError", said, allow(domains.join(", ")));
    }
    const found = new Map();
    for (const domain of domains) {
      for (const cookie of await chrome.cookies.getAll({ domain })) {
        found.set(`${cookie.domain} ${cookie.path} ${cookie.name}`, {
          name: cookie.name,
          value: cookie.value,
          domain: cookie.domain,
          path: cookie.path,
          expires: cookie.expirationDate ?? null,
          secure: cookie.secure,
          httpOnly: cookie.httpOnly,
          sameSite: cookie.sameSite,
        });
      }
    }
    return [...found.values()];
  },

  "tabs.open": async ({ url, active = false }) => {
    await granted(url);
    const tab = await chrome.tabs.create({ url, active });
    const done = await loaded(tab.id);
    const status = await inPage(tab.id, page.status).catch(() => null);
    return { tab: tab.id, url: done?.url ?? url, status };
  },

  "tabs.list": async () => {
    const open = await chrome.tabs.query({});
    return open.map((tab) => ({ tab: tab.id, url: tab.url ?? "", title: tab.title ?? "" }));
  },

  "tabs.close": async ({ tab }) => {
    await chrome.tabs.remove(tab);
    return null;
  },

  "capture.start": async ({ tab, match }) => {
    const pattern = new RegExp(match);
    const id = ++made;
    await attach(tab, `capture ${id}`);
    try {
      await command(tab, "Network.enable");
    } catch (error) {
      await detach(tab, `capture ${id}`);
      throw fail("UsageError", `flnd's extension can't watch tab ${tab}: ${error.message}`);
    }
    captures.set(id, { tab, pattern, seen: new Map(), bodies: [], pending: new Set() });
    return { capture: id };
  },

  "capture.read": async ({ capture }) => {
    const found = captures.get(capture);
    if (!found) throw fail("UsageError", `no capture ${capture} is open`);
    await Promise.allSettled([...found.pending]);
    return found.bodies.splice(0);
  },

  "capture.stop": async ({ capture }) => {
    const found = captures.get(capture);
    if (!found) return null;
    captures.delete(capture);
    await detach(found.tab, `capture ${capture}`);
    return null;
  },

  document: async ({ tab }) => ({ html: await inPage(tab, page.html) }),

  click: async ({ tab, css }) => {
    if (!(await inPage(tab, page.click, css, CLICK))) {
      throw fail("Changed", `nothing on the page matches ${css}`);
    }
    return null;
  },

  scroll: async ({ tab, by }) => ({ height: await inPage(tab, page.scroll, by) }),

  evaluate: async ({ tab, script }) => {
    const reason = `evaluate ${++made}`;
    await attach(tab, reason);
    try {
      const asked = { expression: script, returnByValue: true, awaitPromise: true };
      const found = await command(tab, "Runtime.evaluate", asked);
      if (found.exceptionDetails) {
        const details = found.exceptionDetails;
        const why = details.exception?.description ?? details.text;
        throw fail("Changed", `the page's script failed: ${why}`);
      }
      return { value: found.result?.value ?? null };
    } finally {
      await detach(tab, reason);
    }
  },

  storage: async ({ origin }) => {
    const want = new URL(origin).origin;
    await granted(want);
    const tabs = await chrome.tabs.query({});
    const open = tabs.find((tab) => tab.url && new URL(tab.url).origin === want);
    if (open) return inPage(open.id, page.storage);
    const tab = await chrome.tabs.create({ url: `${want}/`, active: false });
    try {
      await loaded(tab.id);
      return await inPage(tab.id, page.storage);
    } finally {
      await chrome.tabs.remove(tab.id).catch(() => {});
    }
  },

  // captureVisibleTab needs <all_urls> or a click on the extension, so the picture is taken
  // through chrome.debugger, attached for this one call.
  screenshot: async ({ tab }) => {
    const reason = `screenshot ${++made}`;
    await attach(tab, reason);
    try {
      return { png: (await command(tab, "Page.captureScreenshot", { format: "png" })).data };
    } finally {
      await detach(tab, reason);
    }
  },

  media: async ({ tab }) => inPage(tab, page.media),
};

// Ask the host which sites the installed plugins need: give back the permissions none of them
// uses, and return the rest for the popup to ask for. A plugin that was uninstalled loses its
// origins here, the next time the extension connects.
async function needs() {
  const manifest = chrome.runtime.getManifest();
  const { origins = [] } = await chrome.permissions.getAll();
  const optional = origins.filter((each) => !manifest.host_permissions.includes(each));
  const found = await question("needs", { granted: optional });
  if (found.drop?.length) await chrome.permissions.remove({ origins: found.drop }).catch(() => {});
  return found;
}

// ------------------------------------------------------------------------------------------
// The popup
// ------------------------------------------------------------------------------------------

function question(op, args) {
  const id = ++asked;
  return new Promise((resolve, reject) => {
    questions.set(id, { resolve, reject });
    setTimeout(() => {
      if (questions.delete(id)) reject(new Error("flnd didn't answer"));
    }, ANSWER);
    try {
      send({ id, op, args });
    } catch (error) {
      questions.delete(id);
      reject(error);
    }
  });
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id) return false;
  const { op, args } = message;
  const own = { media: OPS.media, needs };
  const asking = Object.hasOwn(own, op) ? own[op](args) : question(op, args);
  asking.then(
    (value) => respond({ ok: true, value }),
    (error) => respond({ ok: false, message: String(error.message ?? error), hint: error.hint }),
  );
  return true; // the answer comes later
});

connect();
needs().catch(() => {}); // flnd isn't answering yet: the popup asks again when it opens
