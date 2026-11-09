import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';

const source=readFileSync(new URL('../app/js/whiteboard.js',import.meta.url),'utf8')
  .replace(/^import .*;$/gm,'').replace(/export /g,'');
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
function fixture(){
  const request=deferred(),timers=[],deadlines=[],signals=[],notices=[],link={};
  const api=runInNewContext(source+`\n
    room='atrium';board={epoch:0,revision:1,strokes:[]};
    overlay={querySelector:selector=>selector==='#wb-copper-mode'?{value:'personal'}:link};
    rasterSnapshot=()=>({width:1,height:1,toDataURL:()=>''});
    updateStatus=()=>{};panelNotice=message=>notices.push(message);
    boardFetch=()=>request.promise;
    ({insert:()=>insertCopper(),
      prepare:()=>{insertion={revision:1,mode:'personal',meeting:'',url:'copper://canvas/insert',status_url:'/status'};},
      replace:(newRoom=true)=>{if(newRoom)generation++;insertion={id:'current'};},
      insertion:()=>insertion})`,{
    document:{addEventListener(){}},window:{addEventListener(){}},
    location:{href:''},URL,structuredClone,request,link,notices,
    AbortController,
    fetch:(_url,options)=>{signals.push(options.signal);return request.promise;},
    setTimeout:(fn,ms)=>{if(ms===15000){const timer={fn,cleared:false};deadlines.push(timer);return timer;}timers.push(fn);return fn;},
    clearTimeout:timer=>{if(timer&&typeof timer==='object')timer.cleared=true;},
  });
  return {api,request,timers,deadlines,signals,notices,link};
}
test('a failed snapshot from the old room cannot clear the new room insertion',async()=>{
  const r=fixture(),pending=r.api.insert();r.api.replace();r.notices.length=0;
  r.request.reject(new Error('old room unavailable'));await pending;
  assert.equal(r.api.insertion().id,'current');assert.deepEqual(r.notices,[]);
});
for(const status of [200,410])test(`a delayed Copper status ${status} cannot modify the next room`,async()=>{
  const r=fixture();r.api.prepare();await r.api.insert();
  const poll=r.timers[0]();r.api.replace();r.notices.length=0;
  r.request.resolve({status,ok:status===200,json:async()=>({status:'inserted',receipt:{canvas_name:'Old room',canvas_id:'old'}})});
  await poll;
  assert.equal(r.api.insertion().id,'current');assert.deepEqual(r.notices,[]);
  assert.equal(r.link.textContent,undefined);assert.equal(r.timers.length,1);
});
test('a status body arriving after replacement in the same room cannot confirm the old insertion',async()=>{
  const r=fixture(),body=deferred();r.api.prepare();await r.api.insert();
  r.request.resolve({status:200,ok:true,json:()=>body.promise});
  const poll=r.timers[0]();await new Promise(resolve=>setImmediate(resolve));
  r.api.replace(false);r.notices.length=0;
  body.resolve({status:'inserted',receipt:{canvas_name:'Old drawing',canvas_id:'old'}});await poll;
  assert.equal(r.api.insertion().id,'current');assert.deepEqual(r.notices,[]);
  assert.equal(r.link.textContent,undefined);
});
test('a failed stale status poll cannot schedule another retry',async()=>{
  const r=fixture();r.api.prepare();await r.api.insert();const poll=r.timers[0]();
  r.api.replace();r.request.reject(new Error('offline'));await poll;
  assert.equal(r.timers.length,1);
});
test('the current insertion still confirms an actual Copper receipt',async()=>{
  const r=fixture();r.api.prepare();await r.api.insert();const poll=r.timers[0]();
  r.request.resolve({status:200,ok:true,json:async()=>({status:'inserted',receipt:{canvas_name:'Team canvas',canvas_id:'team'}})});
  await poll;
  assert.match(r.notices.at(-1),/Inserted into Team canvas/);
  assert.equal(r.link.href,'copper://canvas/team');
});

for(const stage of ['fetch','body'])test(`a stalled Copper ${stage} read times out and polling can retry`,async()=>{
  const r=fixture(),body=deferred();r.api.prepare();await r.api.insert();
  if(stage==='body')r.request.resolve({status:200,ok:true,json:()=>body.promise});
  const poll=r.timers[0]();await new Promise(resolve=>setImmediate(resolve));
  r.deadlines[0].fn();await poll;
  assert.equal(r.signals[0].aborted,true);
  assert.equal(r.deadlines[0].cleared,true);
  assert.equal(r.timers.length,2);
  assert.ok(!r.notices.some(n=>n.startsWith('Inserted')));
  const late={status:'inserted',receipt:{canvas_name:'Late canvas',canvas_id:'late'}};
  if(stage==='fetch')r.request.resolve({status:200,ok:true,json:async()=>late});else body.resolve(late);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(r.link.textContent,undefined);
  // A later poll still requires and confirms the actual receipt.
  await r.timers[1]();
  assert.match(r.notices.at(-1),/Inserted into Late canvas/);
  assert.equal(r.deadlines[1].cleared,true);
});
test('an old receipt deadline cannot retry or change the next room',async()=>{
  const r=fixture();r.api.prepare();await r.api.insert();const poll=r.timers[0]();
  r.api.replace();r.notices.length=0;r.deadlines[0].fn();await poll;
  assert.equal(r.api.insertion().id,'current');assert.deepEqual(r.notices,[]);
  assert.equal(r.timers.length,1);
});
