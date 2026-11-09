import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
const source=readFileSync(new URL('../app/js/whiteboard.js',import.meta.url),'utf8').replace(/^import .*;$/gm,'').replace(/export /g,'');
function fixture(){
 const streams=[],requests=[],timers=new Map(),events=new Map();let finishes=0;
 const api=runInNewContext(source+`\nupdateOverlay=()=>{};updateStatus=()=>{};finishStroke=()=>finishedStroke();({
 watch:r=>watchWhiteboard(r,true),connected:()=>connected,
 recovery:()=>{queue=[{action:'undo',epoch:0}];return drain();},
 snapshot:b=>{board=b;queue=[];},board:()=>board})`,{
  document:{addEventListener(){},querySelector:()=>null,body:{classList:{toggle(){}}}},window:{addEventListener:(name,fn)=>events.set(name,fn)},
  EventSource:class{static CLOSED=2;constructor(url){this.url=url;this.readyState=1;streams.push(this);}close(){this.readyState=2;}},
  finishedStroke:()=>finishes++,structuredClone,setTimeout:(fn,ms)=>{timers.set(ms,fn);return ms;},clearTimeout:id=>timers.delete(id),
  controlFetch:async()=>({ok:false,status:409,json:async()=>({detail:'Drawing changed'})}),
  fetch:url=>new Promise(resolve=>requests.push({url,reply:board=>resolve({json:async()=>board})})),
 });
 return {api,streams,requests,timers,events,finishes:()=>finishes};
}
test('old whiteboard stream callbacks cannot change the current room connection or finish its stroke',()=>{
 const r=fixture();r.api.watch('atrium');const old=r.streams[0];r.api.watch('other');
 old.onopen();assert.equal(r.api.connected(),false);
 r.streams[1].onopen();const count=r.finishes();old.onerror();
 assert.equal(r.api.connected(),true);assert.equal(r.finishes(),count);
});
test('a delayed conflict-recovery snapshot cannot replace the new room board',async()=>{
 const r=fixture();r.api.watch('atrium');const recovery=r.api.recovery();
 await new Promise(resolve=>setImmediate(resolve));assert.equal(r.requests.length,1);
 r.api.watch('other');r.api.snapshot({id:'new-room-drawing',revision:7,strokes:[]});
 r.requests[0].reply({id:'old-room-drawing',revision:9,strokes:[]});await recovery;
 assert.equal(r.api.board().id,'new-room-drawing');
});
test('conflict recovery still replaces the board when its room has not changed',async()=>{
 const r=fixture();r.api.watch('atrium');const recovery=r.api.recovery();
 await new Promise(resolve=>setImmediate(resolve));
 r.requests[0].reply({id:'fresh-snapshot',revision:9,strokes:[]});await recovery;
 assert.equal(r.api.board().id,'fresh-snapshot');
});

test('a replaced stream in the same room cannot publish stale status or snapshots',()=>{
 const r=fixture();r.api.watch('atrium');const old=r.streams[0];old.readyState=2;old.onerror();
 r.timers.get(1500)();const live=r.streams[1];
 old.onopen();assert.equal(r.api.connected(),false);
 live.onopen();const count=r.finishes();old.onerror();
 assert.equal(r.api.connected(),true);assert.equal(r.finishes(),count);
 live.onmessage({data:JSON.stringify({kind:'snapshot',id:'latest',active:true,epoch:0,revision:20,strokes:[]})});
 old.onmessage({data:JSON.stringify({kind:'snapshot',id:'stale',active:true,epoch:0,revision:10,strokes:[]})});
 assert.equal(r.api.board().id,'latest');
});


test('a queued retry cannot replace the stream already restored by an online event',()=>{
 const r=fixture();r.api.watch('atrium');const failed=r.streams[0];
 failed.readyState=2;failed.onerror();const retry=r.timers.get(1500);
 r.events.get('online')();assert.equal(r.streams.length,2);
 const restored=r.streams[1];restored.onopen();assert.equal(r.api.connected(),true);
 retry();
 assert.equal(r.streams.length,2,'the old retry must not create a duplicate live stream');
 restored.onmessage({data:JSON.stringify({kind:'snapshot',id:'restored',active:true,epoch:0,revision:3,strokes:[]})});
 assert.equal(r.api.board().id,'restored');
});
