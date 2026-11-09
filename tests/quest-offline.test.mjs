import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../app/quest/offline.js',import.meta.url),'utf8');
const {createOffline}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));

// An in-memory stand-in for the headset's private file system.
function disk(){
  const files=new Map();
  const folder={
    async getFileHandle(name,{create}={}){
      if(!files.has(name)&&!create)throw Error('NotFound');
      return {
        async createWritable(){const parts=[];files.set(name,null);
          return {write:async c=>parts.push(c),close:async()=>files.set(name,Buffer.concat(parts)),abort:async()=>{}};},
        async getFile(){const b=files.get(name);if(!b)throw Error('NotFound');return {size:b.length,bytes:b};},
      };
    },
    async removeEntry(name){files.delete(name);},
    async *keys(){yield* [...files.keys()];},
  };
  return {files,root:{getDirectoryHandle:async()=>folder}};
}
const store=()=>{const m=new Map();return {getItem:k=>m.get(k)??null,setItem:(k,v)=>m.set(k,v)};};
const body=bytes=>new ReadableStream({start(c){c.enqueue(new Uint8Array(bytes));c.close();}});
const respond=(bytes,ok=true)=>({ok,status:ok?200:404,body:body(bytes),headers:{get:()=>String(bytes)}});
const make=(d,storage,fetcher,quota=1e12)=>createOffline({storage,directory:async()=>d.root,fetcher,estimate:async()=>({quota,usage:0})});

test('downloads each demo file once, then plays it from the headset',async()=>{
  const d=disk(),storage=store(),urls=[];
  const off=make(d,storage,async url=>{urls.push(url);return respond(url.includes('ride')?5:3);});
  const library=[{id:'xr-ride01',url:'/media/quest/ride.mp4',bytes:5},{id:'xr-lobby1',url:'/media/quest/lobby.mp4',bytes:3}];
  await off.sync(library,true);await off.sync(library,true);await off.sync(library,true);
  assert.deepEqual(urls,['/media/quest/ride.mp4','/media/quest/lobby.mp4']);
  assert.deepEqual(off.cached().sort(),['xr-lobby1','xr-ride01']);
  assert.equal((await off.file({id:'xr-ride01'})).size,5);
  assert.equal(await off.file({id:'xr-other1'}),null);
  // The list survives an app restart.
  assert.deepEqual(make(d,storage,async()=>respond(0)).cached().sort(),['xr-lobby1','xr-ride01']);
});
test('waits while a streamed video plays, and removes files no demo uses',async()=>{
  const d=disk(),storage=store();let calls=0;
  const off=make(d,storage,async()=>{calls++;return respond(4);});
  await off.sync([{id:'xr-a0001',url:'/a',bytes:4}],false);
  assert.equal(calls,0);
  await off.sync([{id:'xr-a0001',url:'/a',bytes:4}],true);
  assert.deepEqual(off.cached(),['xr-a0001']);
  await off.sync([],true);
  assert.deepEqual(off.cached(),[]);assert.equal(d.files.size,0);
});
test('a failed or oversized download leaves nothing behind and reports why',async()=>{
  const d=disk(),storage=store();
  const off=make(d,storage,async()=>respond(2,false));
  await off.sync([{id:'xr-bad001',url:'/x',bytes:2}],true);
  assert.match(off.error(),/Download failed/);assert.deepEqual(off.cached(),[]);assert.equal(d.files.size,0);
  const full=make(disk(),store(),async()=>respond(9),100);
  await full.sync([{id:'xr-big001',url:'/y',bytes:9e9}],true);
  assert.match(full.error(),/Not enough space/);
});
test('the viewer hands the module a bound fetch (window.fetch throws when called as a method)',()=>{
  const viewer=readFileSync(new URL('../app/quest/viewer.js',import.meta.url),'utf8');
  assert.match(viewer,/fetcher:\(\.\.\.a\)=>fetch\(\.\.\.a\)/);
});
