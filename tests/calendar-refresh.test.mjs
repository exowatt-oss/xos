import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {runInNewContext} from "node:vm";
import {readCalendar} from "../app/js/calendar-read.js";
const pause=()=>new Promise(r=>setTimeout(r,0));
function load(name, timeoutMs=15000) {
  const listeners={},timers={};
  let reads=0,paint=0,reply=null;
  const document={visibilityState:"visible",addEventListener:(key,fn)=>{listeners[key]=fn;}};
  const location={hash:name==="remote"?"#/tablet/atrium":"#/tv/atrium"};
  const request=async()=>{
    reads++;
    if(reply)await reply;
    return {ok:true,json:async()=>({configured:true,calendar:"room",bookings:[{title:"Read "+reads}]})};
  };
  const context={document,location,window:{addEventListener:(key,fn)=>{listeners[key]=fn;}},setInterval:(fn,ms)=>{timers[ms]=fn;},
    readCalendar:(url,primary,fallback)=>readCalendar(url,primary,fallback,timeoutMs),
    S:{poke(){paint++;}},controlFetch:request,fetch:request};
  const source=readFileSync(new URL("../app/js/views/"+name+".js",import.meta.url),"utf8").replace(/^import .*;$/gm,"").replace(/export function /g,"function ");
  const api=runInNewContext(source+(name==="remote"?"\n({read:()=>refreshCal('atrium'),get:()=>calInfo.atrium})":"\n({read:()=>roomCalendar('atrium'),get:()=>calendars.atrium.data})"),context);
  return {api,listeners,timers,document,location,reads:()=>reads,paints:()=>paint,hold:p=>{reply=p;}};
}
for(const name of ["remote","screens"]) {
  test(name+" calendar refreshes after wake/reconnect, deduplicates events, and keeps the minute cadence",async()=>{
    const x=load(name);
    await x.api.read();await pause();
    assert.equal(x.reads(),1);
    x.document.visibilityState="hidden";
    x.listeners.visibilitychange();
    assert.equal(x.reads(),1);
    x.document.visibilityState="visible";
    let release;x.hold(new Promise(r=>{release=r;}));
    x.listeners.visibilitychange();x.listeners.focus();x.listeners.online();x.listeners.pageshow();
    assert.equal(x.reads(),2,"wake event burst uses a single in-flight read");
    release();x.hold(null);await pause();
    assert.equal(x.api.get().bookings[0].title,"Read 2");
    x.timers[name==="screens"?5000:60000]();await pause();
    assert.equal(x.reads(),3);
    assert.ok(x.paints()>=3);
    x.location.hash="#/admin/screens";
    x.listeners.focus();x.listeners.online();x.timers[name==="screens"?5000:60000]();
    assert.equal(x.reads(),3,"leaving the room view stops its refresh triggers");
  });
}

for(const name of ["remote","screens"]) {
  test(name+" abandons a stalled read and recovers on the next refresh",async()=>{
    const x=load(name,15);
    x.hold(new Promise(()=>{}));
    const reading=x.api.read();
    await new Promise(r=>setTimeout(r,40));
    assert.equal(x.api.get()?.error,true,"deadline releases the loading state");
    await reading;
    x.hold(null);
    x.listeners.online();await pause();
    assert.equal(x.api.get().bookings[0].title,"Read 2");
  });
}

test("calendar deadline includes a stalled JSON body and aborts its request",async()=>{
  let signal;
  await assert.rejects(readCalendar('/calendar',async(_,options)=>{
    signal=options.signal;return {ok:true,json:()=>new Promise(()=>{})};
  },null,10),/timed out/);
  assert.equal(signal.aborted,true);
});
test("calendar fallback retains the same deadline and cancels the timer on success",async()=>{
  let firstSignal,secondSignal;
  const result=await readCalendar('/calendar',async(_,o)=>{firstSignal=o.signal;throw Error('Signed out');},async(_,o)=>{
    secondSignal=o.signal;return {ok:true,json:async()=>({bookings:[]})};
  },10);
  assert.deepEqual(result,{bookings:[]});assert.equal(firstSignal,secondSignal);
  await new Promise(r=>setTimeout(r,20));assert.equal(firstSignal.aborted,false);
});
