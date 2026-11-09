import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import {members} from '../app/js/groups.js';
const source=readFileSync(new URL('../app/js/views/remote.js',import.meta.url),'utf8').replace(/^import .*;$/gm,'').replace(/export function /g,'function ');
const oldURL='https://meet.google.com/aaa-bbbb-ccc',newURL='https://meet.google.com/ddd-eeee-fff';
function fixture(){
 const requests=[],timers=new Map();
 const api=runInNewContext(source+`\nzoomInfo.atrium={};({
 select:(url,live=true,pc='room-pc')=>{devices=[{id:pc,room:'atrium',role:'roompc',online:true,native:{meeting_providers:['meet'],meeting:{provider:'meet',title:'Local test meeting',join_url:url,in_call:live,muted:true,camera_off:true}}}];},
 render:()=>renderRoomRemote('atrium'),open:()=>{sheet='share';return loadInvite('atrium');},
 state:()=>meetingInvites.atrium,read:()=>loadInvite('atrium'),pending:()=>inviteLoading.size})`,{
  window:{addEventListener(){}},document:{addEventListener(){}},setInterval(){},AbortController,
  setTimeout:(fn,ms)=>{const id={ms};timers.set(id,fn);return id;},clearTimeout:id=>timers.delete(id),
  S:{rooms:()=>[{id:'atrium',name:'Atrium'}],room:()=>({name:'Atrium'}),isHub:()=>true,poke(){}},
  location:{hash:'#/tablet/atrium',host:'localhost'},EventSource:class{static CLOSED=2;constructor(){this.readyState=1;}},
  members,displayMembers:()=>[],esc:String,clock:()=>'',
  controlFetch:(url,options)=>url.includes('/meeting-controls')?Promise.resolve({ok:true,json:async()=>({controls:[]})}):new Promise(resolve=>requests.push({url,signal:options?.signal,respond:resolve,reply:url=>resolve({ok:true,json:async()=>({url,qr:'test-qr'})})})),
 });
 return {api,requests,timers};
}
test('an open share panel removes the old invitation and fetches the new meeting even with the same title',async()=>{
 const {api,requests}=fixture();api.select(oldURL);api.render();const first=api.open();requests[0].reply(oldURL);await first;
 assert.match(api.render(),/aaa-bbbb-ccc/);
 api.select(newURL);const html=api.render();assert.doesNotMatch(html,/aaa-bbbb-ccc/);
 assert.equal(requests.length,2);assert.equal(api.state().loading,true);
 requests[1].reply(newURL);await new Promise(r=>setImmediate(r));
 assert.match(api.render(),/ddd-eeee-fff/);
});
test('a late old invitation cannot overwrite a newer meeting or computer response',async()=>{
 const {api,requests}=fixture();api.select(oldURL);api.render();const old=api.open();
 api.select(newURL,true,'new-pc');api.render();const current=api.read();
 assert.equal(requests.length,2);assert.equal(requests[0].signal.aborted,true);
 requests[1].reply(newURL);await current;requests[0].reply(oldURL);await old;
 assert.equal(api.state().url,newURL);
});
test('ending a meeting clears its invitation and ignores late replies',async()=>{
 const {api,requests}=fixture();api.select(oldURL);api.render();const pending=api.open();
 api.select('',false);api.render();requests[0].reply(oldURL);await pending;
 assert.equal(api.state(),undefined);assert.equal(requests[0].signal.aborted,true);
});

test('a stalled invitation fetch releases loading for retry and rejects a late old reply',async()=>{
 const {api,requests,timers}=fixture();api.select(oldURL);api.render();const old=api.open();
 await new Promise(r=>setImmediate(r));
 assert.equal(timers.size,1);assert.equal([...timers.keys()][0].ms,15000);
 timers.values().next().value();await old;
 assert.equal(api.state().error,true);assert.equal(api.pending(),0);assert.equal(requests[0].signal.aborted,true);
 const html=api.render();assert.doesNotMatch(html,/Getting meeting link/);
 const retryButton=html.match(/<button[^>]*data-act="rm-invite-refresh"[^>]*>/)?.[0];
 assert.ok(retryButton);assert.doesNotMatch(retryButton,/\bdisabled\b/);
 const next=api.read();requests[1].reply(newURL);await next;
 requests[0].reply(oldURL);await new Promise(r=>setImmediate(r));
 assert.equal(api.state().url,newURL);assert.equal(timers.size,0);
});
test('the invitation deadline covers a stalled response body',async()=>{
 const {api,requests,timers}=fixture();api.select(oldURL);api.render();const reading=api.open();
 let replyBody;requests[0].respond({ok:true,json:()=>new Promise(resolve=>{replyBody=resolve;})});
 await new Promise(r=>setImmediate(r));timers.values().next().value();await reading;
 assert.equal(api.state().error,true);assert.equal(api.pending(),0);assert.equal(requests[0].signal.aborted,true);
 const retry=api.read();requests[1].reply(newURL);await retry;
 replyBody({url:oldURL,qr:'stale'});await new Promise(r=>setImmediate(r));
 assert.equal(api.state().url,newURL);assert.equal(timers.size,0);
});
test('an old invitation deadline cannot clear a newer meeting read',async()=>{
 const {api,requests,timers}=fixture();api.select(oldURL);api.render();const old=api.open();
 await new Promise(r=>setImmediate(r));const expire=timers.values().next().value;
 api.select(newURL,true,'new-pc');api.render();const next=api.read();
 expire();await old;assert.equal(api.state().loading,true);assert.equal(api.pending(),1);
 requests[1].reply(newURL);await next;
 assert.equal(api.state().url,newURL);assert.equal(timers.size,0);
});
