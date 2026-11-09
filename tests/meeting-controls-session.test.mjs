import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
const source=readFileSync(new URL('../app/js/views/remote.js',import.meta.url),'utf8').replace(/^import .*;$/gm,'').replace(/export function /g,'function ');
function fixture(){
 const requests=[],timers=new Map();
 const api=runInNewContext(source+`\n({select:(pc,session)=>{roomPc.atrium=pc;controlsSession.atrium=session;roomControls.atrium=null;},load:()=>loadControls('atrium'),state:()=>roomControls.atrium,pending:()=>controlsLoading.size})`,{
  window:{addEventListener(){}},document:{addEventListener(){}},setInterval(){},AbortController,
  setTimeout:fn=>{const id={};timers.set(id,fn);return id;},clearTimeout:id=>timers.delete(id),
  S:{poke(){}},controlFetch:(url,options)=>new Promise(resolve=>requests.push({url,signal:options?.signal,respond:resolve,reply:controls=>resolve({ok:true,json:async()=>({controls})})})),
 });
 return {api,requests,timers};
}
test('a previous meeting response cannot replace current controls or clear its pending read',async()=>{
 const {api,requests}=fixture();api.select('room-pc','old-session');const old=api.load();
 api.select('room-pc','new-session');const current=api.load();
 assert.equal(requests.length,2);assert.equal(requests[0].signal.aborted,true);
 requests[0].reply([{id:'old',label:'Previous meeting option'}]);await old;
 assert.equal(api.state(),null);assert.equal(api.pending(),1);
 requests[1].reply([{id:'current',label:'Current meeting option'}]);await current;
 assert.equal(api.state().controls[0].id,'current');assert.equal(api.pending(),0);
});
test('switching room computers rejects the old response even with the same meeting label',async()=>{
 const {api,requests}=fixture();api.select('old-pc','same');const old=api.load();
 api.select('new-pc','same');const current=api.load();
 assert.equal(requests.length,2);
 requests[1].reply([{id:'new',label:'Current option'}]);await current;
 requests[0].reply([{id:'old',label:'Old option'}]);await old;
 assert.equal(api.state().controls[0].id,'new');
});
test('reads in the same meeting remain deduplicated and ended meetings discard late controls',async()=>{
 const {api,requests}=fixture();api.select('room-pc','session');const a=api.load(),b=api.load();
 assert.equal(requests.length,1);api.select('room-pc','');
 requests[0].reply([{id:'old',label:'Old option'}]);
 assert.equal(await a,null);assert.equal(await b,null);assert.equal(api.state(),null);
});

test('a stalled controls fetch times out, allows retry, and ignores its late reply',async()=>{
 const {api,requests,timers}=fixture();api.select('room-pc','session');const old=api.load();
 timers.values().next().value();await old;
 assert.equal(api.state().error,true);assert.equal(api.pending(),0);
 assert.equal(requests[0].signal.aborted,true);assert.equal(timers.size,0);
 const next=api.load();requests[1].reply([{id:'current',label:'Current control'}]);await next;
 requests[0].reply([{id:'stale',label:'Stale control'}]);await new Promise(resolve=>setImmediate(resolve));
 assert.equal(api.state().controls[0].id,'current');assert.equal(timers.size,0);
});
test('a stalled controls body is included in the deadline',async()=>{
 const {api,requests,timers}=fixture();api.select('room-pc','session');const reading=api.load();
 requests[0].respond({ok:true,json:()=>new Promise(()=>{})});await new Promise(resolve=>setImmediate(resolve));
 timers.values().next().value();await reading;
 assert.equal(api.state().error,true);assert.equal(api.pending(),0);assert.equal(requests[0].signal.aborted,true);
});
test('an old deadline cannot mark a newer meeting unavailable or clear its pending read',async()=>{
 const {api,requests,timers}=fixture();api.select('room-pc','old-session');const old=api.load();
 const expire=timers.values().next().value;api.select('room-pc','new-session');const next=api.load();
 expire();await old;assert.equal(api.state(),null);assert.equal(api.pending(),1);
 requests[1].reply([{id:'current',label:'Current control'}]);await next;
 assert.equal(api.state().controls[0].id,'current');assert.equal(timers.size,0);
});
