"""Run native addon callbacks; resource verdicts must precede redirect or method release."""
import json
from pathlib import Path
import shutil
import subprocess

import playwright


def addon(cases, *, table_worker_isolation=False):
    source = Path(__file__).parents[2].joinpath('app/fetching/firefox_policy_addon.js').read_text().replace('/*BRIDGE_CONFIG*/', json.dumps({'origin':'http://127.0.0.1:9','token':'fixture','timeoutMs':500,'tableWorkerIsolation':table_worker_isolation}))
    harness = r'''
const callbacks={}, calls=[], registrations={};
let bodyCopies=0;
const event = name => ({addListener:(callback,filter,extra=[])=>{
 (registrations[name] ||= []).push({callback,filter,extra});
 callbacks[name]=async details=>{
  let result={};
  for(const item of registrations[name]) {
   if(item.filter?.types && !item.filter.types.includes(details.type))continue;
   if(item.extra.includes('requestBody'))bodyCopies++;
   const value=await item.callback(details)||{};
   result={...result,...value};
   if(value.cancel===true)result.cancel=true;
  }
  return result;
 };
}});
globalThis.browser={tabs:{onUpdated:event('updated'),onRemoved:event('removed')},runtime:{getURL:()=> 'moz-extension://trusted/'},
 webRequest:Object.fromEntries(['onBeforeRequest','onBeforeSendHeaders','onHeadersReceived','onBeforeRedirect','onCompleted','onErrorOccurred'].map(n=>[n,event(n)])),
 extension:{isAllowedIncognitoAccess:async()=>true}};
globalThis.fetch=async(url,options)=>{const p=JSON.parse(options.body);calls.push([url,p]);
 return {ok:true,json:async()=>({allow:p.method !== 'POST' && !String(p.url).includes('internal.test'),generation:p.generation})};};
'''
    node = shutil.which('node') or str(Path(playwright.__file__).parent/'driver'/'node')
    result = subprocess.run([node, '-'], input=harness+source+cases, text=True, capture_output=True, timeout=5, check=True)
    return json.loads(result.stdout)


def test_firefox_table_resource_post_and_redirect_are_native_denials():
    assert addon(r'''
(async()=>{
 const b={storeId:'private-1',generation:'g'};tabs.set(1,b);tableBindings.set(1,b);
 const d={tabId:1,type:'xmlhttprequest',method:'POST',requestId:'p',url:'https://catalogue.test/write'};
 const post=await callbacks.onBeforeRequest(d);
 const redirect=await callbacks.onHeadersReceived({...d,method:'GET',requestId:'g',statusCode:302,responseHeaders:[{name:'Location',value:'https://internal.test/private'}]});
 process.stdout.write(JSON.stringify([post.cancel,redirect.cancel]));
})();
''') == [True, True]


def test_firefox_table_unknown_worker_is_denied_but_own_control_is_not():
    assert addon(r'''
(async()=>{
 const b={storeId:'private-1',generation:'g'};tabs.set(1,b);tableBindings.set(1,b);
 const d={tabId:-1,type:'xmlhttprequest',method:'POST',requestId:'w',url:'http://127.0.0.1:9/table-control'};
 const worker=await callbacks.onBeforeRequest({...d,originUrl:'https://catalogue.test/worker.js'});
 const own=await callbacks.onBeforeRequest({...d,originUrl:'moz-extension://trusted/background.js'});
 process.stdout.write(JSON.stringify([worker.cancel,own.cancel===true]));
})();
''') == [True, False]


def test_activation_enrolls_a_get_already_started_before_the_ack():
    assert addon(r'''
(async()=>{
 const b={storeId:'private-1',generation:'g'};tabs.set(1,b);
 await callbacks.onBeforeRequest({tabId:1,type:'xmlhttprequest',method:'GET',requestId:'pending',url:'https://catalogue.test/data'});
 const policies=[];
 globalThis.fetch=async(url,options)=>{const p=JSON.parse(options.body);
   if(url.endsWith('/table-policy')) policies.push(p.requestId);
   if(url.endsWith('/table-ack')){process.stdout.write(JSON.stringify(policies));process.exit(0);}
   return {ok:true,json:async()=>url.endsWith('/table-control')?{allow:true,revision:1,tables:[{tabId:1,...b}]}:{allow:true,generation:p.generation}};
 };
 void tableControl();
})();
''') == ['pending']


def test_activation_does_not_adopt_a_preexisting_speculative_favicon():
    assert addon(r'''
(async()=>{
 const b={storeId:'private-1',generation:'g'};tabs.set(1,b);
 await callbacks.onBeforeRequest({tabId:1,type:'image',method:'GET',requestId:'favicon',url:'https://catalogue.test/favicon.ico'});
 const policies=[];
 globalThis.fetch=async(url,options)=>{const p=JSON.parse(options.body);
   if(url.endsWith('/table-policy'))policies.push(p.requestId);
   if(url.endsWith('/table-ack')){process.stdout.write(JSON.stringify(policies));process.exit(0);}
   return {ok:true,json:async()=>url.endsWith('/table-control')?{allow:true,revision:1,tables:[{tabId:1,...b}]}:{allow:true,generation:p.generation}};
 };
 void tableControl();
})();
''') == []


def test_status_302_without_location_is_not_a_redirect_policy_denial():
    assert addon(r'''
(async()=>{
 const b={storeId:'private-1',generation:'g'};tabs.set(1,b);
 const d={tabId:1,type:'xmlhttprequest',method:'GET',requestId:'plain',url:'https://catalogue.test/data',statusCode:302,responseHeaders:[]};
 const ordinary=await callbacks.onHeadersReceived(d);
 tableBindings.set(1,b);
 const table=await callbacks.onHeadersReceived(d);
 process.stdout.write(JSON.stringify([ordinary.cancel===true,table.cancel===true]));
})();
''') == [False,False]


def test_dedicated_launch_csp_prevents_workers_before_first_site_script():
    assert addon(r'''
(async()=>{
 const b={storeId:'private-1',generation:'g'};tabs.set(1,b);
 const d={tabId:1,type:'main_frame',method:'GET',requestId:'initial',url:'https://catalogue.test/product',statusCode:200,responseHeaders:[{name:'Content-Security-Policy',value:"default-src 'self'"}]};
 const result=await callbacks.onHeadersReceived(d);
 process.stdout.write(JSON.stringify(result.responseHeaders || []));
})();
''',table_worker_isolation=True)==[{'name':'Content-Security-Policy','value':"default-src 'self'"},{'name':'Content-Security-Policy','value':"worker-src 'none'"}]


def test_non_document_uploads_do_not_request_native_body_materialization():
    assert addon(r'''
(async()=>{
 const b={storeId:'private-1',generation:'g'};tabs.set(1,b);
 const data={tabId:1,type:'xmlhttprequest',method:'POST',requestId:'upload',url:'https://catalogue.test/upload'};
 const upload=await callbacks.onBeforeRequest(data);
 const uploadCopies=bodyCopies;
 await callbacks.onBeforeRequest({...data,type:'main_frame',method:'GET',requestId:'doc',url:'https://catalogue.test/product'});
 const policies=calls.filter(([url])=>url.endsWith('/policy'));
 process.stdout.write(JSON.stringify([upload.cancel===true,uploadCopies,bodyCopies,policies.length]));
})();
''') == [False,0,1,1]
