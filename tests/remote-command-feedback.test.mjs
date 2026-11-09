import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
const source=readFileSync(new URL('../app/js/views/remote.js',import.meta.url),'utf8').replace(/^import .*;$/gm,'').replace(/export function /g,'function ');
function remote(fetcher){
  let now=0,reads=0;const intervals=new Map(),timeouts=new Map();
  const api=runInNewContext(source+`\nes={readyState:1};({wait:(ms=8000)=>waitFor('pc','command',ms),event:c=>waiters.get('command')?.(c)})`,{
    window:{addEventListener(){}},document:{addEventListener(){}},EventSource:{OPEN:1},
    AbortController,Date:{now:()=>now},setInterval:(fn,ms)=>{intervals.set(ms,fn);return ms;},
    clearInterval:id=>intervals.delete(id),setTimeout:(fn,ms)=>{timeouts.set(ms,fn);return ms;},clearTimeout:id=>timeouts.delete(id),
    fetch:async(...args)=>{reads++;return fetcher?fetcher(...args):{json:async()=>[{id:'command',status:'done'}]};},
  });
  return {api,intervals,timeouts,reads:()=>reads,at:t=>now=t,tick:()=>intervals.get(1200)?.()};
}
test('lost completion event is confirmed from history even while the live stream stays open',async()=>{
  const r=remote(),result=r.api.wait();
  r.at(1200);await r.tick();assert.equal(r.reads(),0,'allow live events to arrive first');
  r.at(3600);await r.tick();assert.equal(r.reads(),1);
  assert.equal((await result).status,'done');assert.equal(r.intervals.has(1200),false);
});
test('a prompt live completion avoids a history request',async()=>{
  const r=remote(),result=r.api.wait();r.api.event({id:'command',status:'done'});
  assert.equal((await result).status,'done');assert.equal(r.reads(),0);
});
test('history polling does not overlap and is aborted when its wait ends',async()=>{
  let signal;const r=remote(async(_,options)=>{signal=options.signal;return new Promise(()=>{});});
  const result=r.api.wait();r.at(3600);r.tick();await Promise.resolve();
  r.at(4800);r.tick();assert.equal(r.reads(),1);
  r.timeouts.get(8000)();assert.equal(await result,null);assert.equal(signal.aborted,true);
  assert.equal(r.intervals.has(1200),false);
});
