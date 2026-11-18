// flnd's popup: asks flnd, through the service worker and the host, what this tab is
// (src/docs/extension.md).

const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

async function ask(op, args) {
  const answer = await chrome.runtime.sendMessage({ op, args });
  if (!answer?.ok) {
    const said = new Error(answer?.message ?? "flnd didn't answer");
    throw Object.assign(said, { hint: answer?.hint ?? "" });
  }
  return answer.value;
}

function line(text, quiet = false) {
  const shown = document.createElement("p");
  shown.textContent = text;
  if (quiet) shown.className = "quiet";
  return shown;
}

function row(text, ...buttons) {
  const shown = document.createElement("div");
  shown.className = "row";
  const label = document.createElement("span");
  label.textContent = text;
  label.title = text;
  shown.append(label, ...buttons);
  return shown;
}

// A button that asks flnd `op` about `target`, and says what came of it.
function button(name, op, target, status) {
  const made = document.createElement("button");
  made.textContent = name;
  made.addEventListener("click", async () => {
    made.disabled = true;
    try {
      await ask(op, { target });
      status.textContent = `${name}: started in flnd`;
    } catch (error) {
      status.textContent = `${error.message}${error.hint ? ` (${error.hint})` : ""}`;
      made.disabled = false;
    }
  });
  return made;
}

async function showTarget() {
  const place = document.getElementById("target");
  const status = line("", true);
  try {
    const found = await ask("resolve", { url: tab.url });
    const follow = found.followed ? [] : [button("Follow", "follow", found.target, status)];
    const get = button("Get", "get", found.target, status);
    place.replaceChildren(row(found.target, get, ...follow), status);
    if (found.followed) status.textContent = "followed";
  } catch (error) {
    place.replaceChildren(line(error.message), line(error.hint ?? "", true));
  }
}

async function showMedia() {
  const place = document.getElementById("media");
  const status = line("", true);
  let found;
  try {
    found = await ask("media", { tab: tab.id });
  } catch (error) {
    place.replaceChildren(line(error.message, true));
    return;
  }
  if (!found?.length) {
    place.replaceChildren(line("none on this page", true));
    return;
  }
  const items = found.map((item) => {
    const entry = document.createElement("li");
    entry.append(row(`${item.kind}: ${item.url}`, button("Get", "get", item.url, status)));
    return entry;
  });
  place.replaceChildren(...items, status);
}

// The site's optional permission, when flnd's plugins may read it but Chrome hasn't allowed it.
async function showAllow() {
  if (!tab.url?.startsWith("http")) return;
  const host = new URL(tab.url).hostname;
  const optional = chrome.runtime.getManifest().optional_host_permissions ?? [];
  const pattern = optional.find((each) => {
    const domain = each.replace(/^\*:\/\/\*\./, "").replace(/\/\*$/, "");
    return host === domain || host.endsWith(`.${domain}`);
  });
  if (!pattern || (await chrome.permissions.contains({ origins: [pattern] }))) return;
  document.getElementById("allow-text").textContent = `Let flnd read ${host}`;
  document.getElementById("allow").hidden = false;
  document.getElementById("allow-button").addEventListener("click", async () => {
    if (await chrome.permissions.request({ origins: [pattern] })) {
      document.getElementById("allow").hidden = true;
    }
  });
}

showAllow();
showTarget();
showMedia();
