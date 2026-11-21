/* Native document transport stays inside Firefox. No document is fetched here. */
"use strict";
const config = /*BRIDGE_CONFIG*/;
const tabs = new Map();
const enrollments = new Map();
const requests = new Map();
const prefix = "about:blank#wss-document-policy=";
async function call(path, payload) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), config.timeoutMs);
  try {
    const response = await fetch(config.origin + path, {
      method: "POST", redirect: "error", cache: "no-store", credentials: "omit",
      headers: {"Authorization": "Bearer " + config.token, "Content-Type": "application/json"},
      body: JSON.stringify(payload), signal: controller.signal,
    });
    if (!response.ok) throw new Error("Policy bridge unavailable");
    return await response.json();
  } finally { clearTimeout(timer); }
}
function enroll(tabId, url) {
  if (typeof url !== "string" || !url.startsWith(prefix)) return;
  const nonce = url.slice(prefix.length);
  if (!/^[A-Za-z0-9_-]{43}$/.test(nonce) || enrollments.get(tabId) === nonce) return;
  tabs.delete(tabId);
  enrollments.set(tabId, nonce);
  (async () => {
    try {
      const tab = await browser.tabs.get(tabId);
      const storeId = tab.cookieStoreId;
      const result = await call("/enroll", {nonce, tabId, storeId});
      if (enrollments.get(tabId) !== nonce || result.allow !== true) return;
      const binding = {storeId, generation: result.generation};
      tabs.set(tabId, binding);
      const ack = await call("/bound", {tabId, ...binding});
      if (ack.allow !== true && tabs.get(tabId) === binding) tabs.delete(tabId);
    } catch (_) { tabs.delete(tabId); }
  })();
}
browser.tabs.onUpdated.addListener((tabId, change) => enroll(tabId, change.url));
browser.tabs.onRemoved.addListener(tabId => { tabs.delete(tabId); enrollments.delete(tabId); });
browser.webRequest.onBeforeRequest.addListener(async details => {
  const binding = tabs.get(details.tabId);
  if (!binding) return {cancel: true};
  if (!requests.has(details.requestId)) {
    if (requests.size >= 1024) return {cancel: true};
    requests.set(details.requestId, 0);
  }
  try {
    const result = await call("/policy", {
      tabId: details.tabId, ...binding, url: details.url, frameKind: details.type,
      requestId: details.requestId, redirects: requests.get(details.requestId),
    });
    return {cancel: !(result.allow === true && result.generation === binding.generation && tabs.get(details.tabId) === binding)};
  } catch (_) { return {cancel: true}; }
}, {urls: ["<all_urls>"], types: ["main_frame", "sub_frame"]}, ["blocking"]);
// Firefox may omit onBeforeRequest for non-network redirect targets. Check
// Location while the native response is still blockable, before following it.
browser.webRequest.onHeadersReceived.addListener(details => {
  if (![301, 302, 303, 307, 308].includes(details.statusCode)) return {};
  const locations = (details.responseHeaders || []).filter(header => header.name.toLowerCase() === "location");
  if (!locations.length) return {};
  if (locations.length !== 1 || typeof locations[0].value !== "string") return {cancel: true};
  try {
    const target = new URL(locations[0].value, details.url);
    return {cancel: !["http:", "https:"].includes(target.protocol)};
  } catch (_) { return {cancel: true}; }
}, {urls: ["<all_urls>"], types: ["main_frame", "sub_frame"]}, ["blocking", "responseHeaders"]);
browser.webRequest.onBeforeRedirect.addListener(details => {
  // Playwright's native interception can restart the same channel with a
  // status-zero internal redirect. Only HTTP hops consume the redirect budget
  // or turn the caller-admitted original URL into a new destination.
  if ([301, 302, 303, 307, 308].includes(details.statusCode) && requests.has(details.requestId)) {
    requests.set(details.requestId, requests.get(details.requestId) + 1);
  }
}, {urls: ["<all_urls>"], types: ["main_frame", "sub_frame"]});
for (const event of [browser.webRequest.onCompleted, browser.webRequest.onErrorOccurred]) {
  event.addListener(details => requests.delete(details.requestId), {urls: ["<all_urls>"], types: ["main_frame", "sub_frame"]});
}
(async () => {
  try { await call("/ready", {privateAccess: await browser.extension.isAllowedIncognitoAccess()}); }
  catch (_) { /* Remain fail closed; launch readiness times out. */ }
})();
