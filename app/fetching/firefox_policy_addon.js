/* Native document transport stays inside Firefox. No document is fetched here. */
"use strict";
const config = /*BRIDGE_CONFIG*/;
const tabs = new Map();
const enrollments = new Map();
const requests = new Map();
const formRequests = new Map();
const tableBindings = new Map();
const tableRequests = new Map();
const observedResources = new Map();
let resourceOverflow = false;
const extensionOrigin = browser.runtime?.getURL ? browser.runtime.getURL("") : "";
const prefix = "about:blank#wss-document-policy=";
function opaqueForm(body) {
  if (!body || body.error || body.raw || !body.formData) return null;
  const entries = Object.entries(body.formData);
  if (entries.length < 1 || entries.length > 8) return null;
  const encoded = new URLSearchParams();
  for (const [name, values] of entries) {
    if (!/^[a-f0-9]{64}$/.test(name) || !Array.isArray(values) || values.length !== 1) return null;
    const value = values[0];
    if (typeof value !== "string" || value.length < 1 || value.length > 16384 || !/^[\x20-\x7e]+$/.test(value)) return null;
    encoded.append(name, value);
  }
  const text = encoded.toString();
  return text.length > 0 && text.length <= 65536 ? {encoded: text, size: text.length} : null;
}
function oneHeader(headers, name) {
  const found = (headers || []).filter(h => h.name.toLowerCase() === name);
  return found.length === 1 && typeof found[0].value === "string" ? found[0].value : null;
}
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
function ownControl(details) {
  return details.tabId === -1 && extensionOrigin && typeof details.originUrl === "string" &&
    details.originUrl.startsWith(extensionOrigin) && details.url.startsWith(config.origin + "/");
}
async function malformedRedirect(details, binding) {
  try {await call("/private-redirect",{url:details.url,invalid:true,...(binding?{tabId:details.tabId,...binding}:{})});}
  catch (_) {/* The native request is denied even if error attribution fails. */}
  return {cancel:true};
}
async function tableVerdict(details, binding, url, stage) {
  try {
    const result = await call("/table-policy", {tabId:details.tabId,...binding,url,
      resourceKind:details.type,requestId:details.requestId,method:details.method,
      redirects:requests.get(details.requestId) || 0,stage});
    return result.allow === true && result.generation === binding.generation && tableBindings.get(details.tabId) === binding;
  } catch (_) {return false;}
}
async function tableControl() {
  let after = -1;
  while (true) {
    try {
      const result = await call("/table-control", {after:Math.max(0,after)});
      if (result.allow !== true || !Number.isSafeInteger(result.revision) || !Array.isArray(result.tables) || result.tables.length > 128) throw new Error("Table control unavailable");
      const next = new Map();
      for (const item of result.tables) {
        if (!Number.isInteger(item.tabId) || item.tabId < 0 || typeof item.storeId !== "string" || typeof item.generation !== "string") throw new Error("Invalid table binding");
        const old = tableBindings.get(item.tabId);
        next.set(item.tabId, old && old.generation === item.generation && old.storeId === item.storeId ? old : {storeId:item.storeId,generation:item.generation});
      }
      tableBindings.clear();
      for (const [tab,binding] of next) tableBindings.set(tab,binding);
      for (const details of observedResources.values()) {
        const binding=tableBindings.get(details.tabId);
        if (binding && !tableRequests.has(details.requestId)) {
          tableRequests.set(details.requestId,{binding,tabId:details.tabId,kind:details.type});
          requests.set(details.requestId,0);
          await tableVerdict(details,binding,details.url,"request");
        }
      }
      after=result.revision;
      const ack = await call("/table-ack", {revision:after,failed:resourceOverflow});
      if (ack.allow !== true) throw new Error("Table acknowledgment unavailable");
    } catch (_) {
      // Keep restrictive latches across bridge outages; never fall back open.
      await new Promise(resolve => setTimeout(resolve,250));
    }
  }
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
async function requestVerdict(details) {
  if (ownControl(details)) return {};
  if (details.type === "xmlhttprequest" && tabs.has(details.tabId)) {
    if (observedResources.has(details.requestId) || observedResources.size < 1024) observedResources.set(details.requestId,{tabId:details.tabId,type:details.type,method:details.method,requestId:details.requestId,url:details.url});
    else resourceOverflow=true;
  }
  const table = tableBindings.get(details.tabId);
  if (table) {
    if (!tableRequests.has(details.requestId)) {
      if (tableRequests.size >= 1024) return {cancel:true};
      tableRequests.set(details.requestId,{binding:table,tabId:details.tabId,kind:details.type});
      requests.set(details.requestId,0);
    }
    return {cancel:!(await tableVerdict(details,table,details.url,"request"))};
  }
  if (tableBindings.size && details.tabId < 0) {
    // Exclusive browser lease: unknown worker/background traffic has no owner.
    const [tabId,binding] = tableBindings.entries().next().value;
    await tableVerdict({...details,tabId,method:"POST"},binding,details.url,"request");
    return {cancel:true};
  }
  if (!["main_frame","sub_frame"].includes(details.type)) return {};
  const binding = tabs.get(details.tabId);
  if (!binding) return {cancel: true};
  if (!requests.has(details.requestId)) {
    if (requests.size >= 1024) return {cancel: true};
    requests.set(details.requestId, 0);
  }
  try {
    const form = details.type === "main_frame" && details.method === "POST" ? opaqueForm(details.requestBody) : null;
    const formBytes = form === null ? null : form.size;
    const previous = formRequests.get(details.requestId);
    if (previous && (!form || previous.encoded !== form.encoded || previous.binding !== binding || previous.url !== details.url)) return {cancel: true};
    const result = await call("/policy", {
      tabId: details.tabId, ...binding, url: details.url, frameKind: details.type, method: details.method,
      requestId: details.requestId, redirects: requests.get(details.requestId), formBytes,
    });
    const allowed = result.allow === true && result.generation === binding.generation && tabs.get(details.tabId) === binding;
    if (allowed && form !== null && !formRequests.has(details.requestId)) {
      formRequests.set(details.requestId, {binding, tabId: details.tabId, url: details.url, ...form, checked: false});
    }
    return {cancel: !allowed};
  } catch (_) { return {cancel: true}; }
}
// Body capture is needed only for the existing native document clearance form.
// Resource GET-only verdicts must precede any upload-body materialization.
browser.webRequest.onBeforeRequest.addListener(details => {
  if (["main_frame","sub_frame"].includes(details.type)) return {};
  return requestVerdict(details);
}, {urls:["<all_urls>"]}, ["blocking"]);
browser.webRequest.onBeforeRequest.addListener(requestVerdict,
  {urls:["<all_urls>"],types:["main_frame","sub_frame"]}, ["blocking","requestBody"]);
// Body classification and final wire headers belong to the same native request.
// A new generation, multipart/file body, missing/duplicate headers or retry cannot
// use the reservation. No form values are sent to the policy bridge.
browser.webRequest.onBeforeSendHeaders.addListener(async details => {
  if (tableBindings.has(details.tabId) && details.method !== "GET") return {cancel:true};
  if (details.type !== "main_frame" || details.method !== "POST") return {};
  const form = formRequests.get(details.requestId);
  if (!form || form.checked || form.tabId !== details.tabId || form.url !== details.url || tabs.get(details.tabId) !== form.binding) return {cancel: true};
  form.checked = true;
  const type = oneHeader(details.requestHeaders, "content-type");
  const length = oneHeader(details.requestHeaders, "content-length");
  if (!(type && /^application\/x-www-form-urlencoded(?:;\s*charset=(?:utf-8|us-ascii))?$/i.test(type)
    && length && /^[1-9][0-9]{0,5}$/.test(length) && Number(length) === form.size)) return {cancel: true};
  try {
    const result = await call("/form-release", {tabId: details.tabId, ...form.binding, url: form.url,
      requestId: details.requestId, formBytes: form.size});
    return {cancel: !(result.allow === true && result.generation === form.binding.generation
      && tabs.get(details.tabId) === form.binding && formRequests.get(details.requestId) === form)};
  } catch (_) { return {cancel: true}; }
}, {urls: ["<all_urls>"], types: ["main_frame"]}, ["blocking", "requestHeaders"]);
// Firefox may omit onBeforeRequest for non-network redirect targets. Check
// Location while the native response is still blockable, before following it.
async function responseVerdict(details) {
  if (ownControl(details)) return {};
  const table = tableBindings.get(details.tabId);
  if (table) {
    if (![301,302,303,307,308].includes(details.statusCode)) return {};
    if (!(details.responseHeaders || []).some(h=>h.name.toLowerCase() === "location")) return {};
    const location = oneHeader(details.responseHeaders,"location");
    if (location === null) return await malformedRedirect(details,table);
    try {
      const target = new URL(location,details.url).href;
      return {cancel:!(await tableVerdict(details,table,target,"redirect"))};
    } catch (_) {return {cancel:true};}
  }
  if (details.type && !["main_frame","sub_frame"].includes(details.type)) {
    if (![301,302,303,307,308].includes(details.statusCode)) return {};
    if (!(details.responseHeaders || []).some(h=>h.name.toLowerCase() === "location")) return {};
    const location=oneHeader(details.responseHeaders,"location");
    if (location === null) return await malformedRedirect(details,tabs.get(details.tabId));
    try {
      const target=new URL(location,details.url).href;
      const binding=tabs.get(details.tabId);
      const result=await call("/private-redirect",{url:target,...(binding?{tabId:details.tabId,...binding}:{})});
      // Arming may have completed while a global privacy verdict was pending.
      const armed=tableBindings.get(details.tabId);
      return {cancel:result.allow !== true || (armed && !(await tableVerdict(details,armed,target,"redirect")))};
    } catch (_) {return {cancel:true};}
  }
  // A 307/308 must never forward the one permitted POST body to another request,
  // including when Firefox omits onBeforeRequest for a native redirect target.
  if (details.type === "main_frame" && details.method === "POST" && [307, 308].includes(details.statusCode)) return {cancel: true};
  if (details.type === "main_frame" && details.method === "GET" && details.statusCode === 403) {
    const binding = tabs.get(details.tabId);
    if (!binding) return {cancel: true};
    try {
      const result = await call("/challenge", {
        tabId: details.tabId, ...binding, url: details.url, requestId: details.requestId, status: details.statusCode,
        headers: {server: oneHeader(details.responseHeaders, "server"), "cf-mitigated": oneHeader(details.responseHeaders, "cf-mitigated"), "cf-ray": oneHeader(details.responseHeaders, "cf-ray")},
      });
      if (result.allow !== true || result.generation !== binding.generation || tabs.get(details.tabId) !== binding) return {cancel: true};
    } catch (_) { return {cancel: true}; }
  }
  if (![301, 302, 303, 307, 308].includes(details.statusCode)) return {};
  const locations = (details.responseHeaders || []).filter(header => header.name.toLowerCase() === "location");
  if (!locations.length) return {};
  if (locations.length !== 1 || typeof locations[0].value !== "string") return {cancel: true};
  try {
    const target = new URL(locations[0].value, details.url);
    return {cancel: !["http:", "https:"].includes(target.protocol)};
  } catch (_) { return {cancel: true}; }
}
browser.webRequest.onHeadersReceived.addListener(async details => {
  const result = await responseVerdict(details);
  if (config.tableWorkerIsolation === true && ["main_frame","sub_frame"].includes(details.type) && result.cancel !== true) {
    // Dedicated capture browser only: response CSP precedes the first supplier
    // script, including blob workers, and survives normal document replacement.
    return {...result,responseHeaders:[...(result.responseHeaders || details.responseHeaders || []),
      {name:"Content-Security-Policy",value:"worker-src 'none'"}]};
  }
  return result;
}, {urls: ["<all_urls>"]}, ["blocking", "responseHeaders"]);
browser.webRequest.onBeforeRedirect.addListener(details => {
  // Playwright's native interception can restart the same channel with a
  // status-zero internal redirect. Only HTTP hops consume the redirect budget
  // or turn the caller-admitted original URL into a new destination.
  if ([301, 302, 303, 307, 308].includes(details.statusCode) && requests.has(details.requestId)) {
    requests.set(details.requestId, requests.get(details.requestId) + 1);
  }
}, {urls: ["<all_urls>"]});
for (const [event,failed] of [[browser.webRequest.onCompleted,false],[browser.webRequest.onErrorOccurred,true]]) {
  event.addListener(async details => {
    const table=tableRequests.get(details.requestId);
    if (table) {
      try {await call("/table-complete",{tabId:table.tabId,...table.binding,requestId:details.requestId,failed:table.kind === "xmlhttprequest" && (failed || details.statusCode >= 400)});}
      catch (_) {/* A missing completion remains pending and cannot establish quiescence. */}
    }
    requests.delete(details.requestId);formRequests.delete(details.requestId);tableRequests.delete(details.requestId);observedResources.delete(details.requestId);
  }, {urls:["<all_urls>"]});
}
(async () => {
  try {
    const ready=await call("/ready", {privateAccess:await browser.extension.isAllowedIncognitoAccess(),tableWorkerIsolation:config.tableWorkerIsolation === true});
    if (ready.allow === true && ready.tableControl === true) void tableControl();
  } catch (_) {/* Remain fail closed; launch readiness times out. */}
})();
