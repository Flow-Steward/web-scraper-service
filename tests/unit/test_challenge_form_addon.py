"""Run the actual native addon callbacks with an authenticated-bridge stand-in."""
import json
from pathlib import Path
import shutil
import subprocess

import playwright


def run_addon(cases):
    source = Path(__file__).parents[2].joinpath("app/fetching/firefox_policy_addon.js").read_text().replace("/*BRIDGE_CONFIG*/", '{"origin":"https://policy.invalid","token":"fixture","timeoutMs":1000}')
    harness = r'''
const callbacks = {};
const event = name => ({addListener: callback => callbacks[name] = callback});
globalThis.browser = {
  tabs: {onUpdated: event("updated"), onRemoved: event("removed")},
  webRequest: Object.fromEntries(["onBeforeRequest", "onBeforeSendHeaders", "onBeforeRedirect", "onHeadersReceived", "onCompleted", "onErrorOccurred"].map(name => [name, event(name)])),
  extension: {isAllowedIncognitoAccess: async () => true},
};
let finalAllowed = false;
globalThis.fetch = async (url, options) => {
  const payload = JSON.parse(options.body);
  return {ok: true, json: async () => ({allow: !url.endsWith("/form-release") || finalAllowed, generation:payload.generation})};
};
'''
    node = shutil.which("node") or str(Path(playwright.__file__).parent / "driver" / "node")
    result = subprocess.run([node, "-"], input=harness + source + cases, text=True, capture_output=True, check=True, timeout=10)
    return json.loads(result.stdout)


def test_header_gate_honors_backend_revocation_before_wire_send():
    result = run_addon(r'''
(async () => {
  tabs.set(5, {storeId:"private-1",generation:"current"});
  const details = {tabId:5,type:"main_frame",method:"POST",requestId:"post",url:"https://shop.test/product",
    requestBody:{formData:Object.fromEntries([1,2,3].map(n => [String(n).repeat(64),["opaque-proof-"+n]]))}};
  const admitted = await callbacks.onBeforeRequest(details);
  const final = await callbacks.onBeforeSendHeaders({...details,requestHeaders:[
    {name:"Content-Type",value:"application/x-www-form-urlencoded"},{name:"Content-Length",value:"239"}]});
  process.stdout.write(JSON.stringify({admitted:admitted.cancel,final:final.cancel}));
})();
''')
    assert result == {"admitted": False, "final": True}


def test_body_and_header_validation_and_request_binding():
    result = run_addon(r'''
(async () => {
  finalAllowed = true;
  const binding = {storeId:"private-1",generation:"current"};
  tabs.set(5,binding);
  const data = () => Object.fromEntries([1,2,3].map(n => [String(n).repeat(64),["opaque-proof-"+n]]));
  const details = id => ({tabId:5,type:"main_frame",method:"POST",requestId:id,url:"https://shop.test/product",requestBody:{formData:data()}});
  const headers = [{name:"Content-Type",value:"application/x-www-form-urlencoded"},{name:"Content-Length",value:"239"}];
  const send = async (id,h) => {
    const d = details(id);await callbacks.onBeforeRequest(d);
    return (await callbacks.onBeforeSendHeaders({...d,requestHeaders:h})).cancel;
  };
  const decisions = [];
  decisions.push(await send("valid",headers));
  decisions.push((await callbacks.onBeforeSendHeaders({...details("valid"),requestHeaders:headers})).cancel);
  decisions.push(await send("duplicate",[...headers,{name:"content-type",value:"application/x-www-form-urlencoded"}]));
  decisions.push(await send("multipart",[{name:"Content-Type",value:"multipart/form-data"},headers[1]]));
  decisions.push(await send("no-length",[headers[0]]));
  decisions.push(await send("wrong-length",[headers[0],{name:"Content-Length",value:"240"}]));
  await callbacks.onBeforeRequest(details("stale"));tabs.set(5,{storeId:"private-1",generation:"new"});
  decisions.push((await callbacks.onBeforeSendHeaders({...details("stale"),requestHeaders:headers})).cancel);
  tabs.set(5,binding);await callbacks.onBeforeRequest(details("changed"));
  const changed=details("changed");changed.requestBody.formData["1".repeat(64)]=["different-val1"];
  decisions.push((await callbacks.onBeforeRequest(changed)).cancel);
  const duplicate=data();duplicate["1".repeat(64)].push("second");
  const unicode=data();unicode["1".repeat(64)]=["\u00e9"];
  const big=data();big["1".repeat(64)]=["x".repeat(16385)];
  const shapes=[{}, {raw:[],formData:data()}, {formData:{quantity:["1"]}}, {formData:duplicate}, {formData:unicode}, {formData:big}];
  process.stdout.write(JSON.stringify({decisions, rejected:shapes.map(v => opaqueForm(v)===null)}));
})();
''')
    assert result == {"decisions": [False, True, True, True, True, True, True, True], "rejected": [True] * 6}
