import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../app/demos/pairing-state.js',import.meta.url),'utf8');
const {setupChecks,freeSlots,siteChoices}=await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const unit={name:'Site A headset 01',paired:true,enabled:true,online:true,telemetry:{visibility:'visible',immersive:true},preview_at:99};
const ready=(u,enabled=true,preview=true,now=100)=>setupChecks(u,enabled,preview,now).every(c=>c.ok);

test('a pairing acknowledgement alone never reports a headset ready',()=>{
  assert.equal(ready({...unit,online:false,telemetry:{},preview_at:0}),false);
  assert.equal(ready({...unit,telemetry:{visibility:'visible',immersive:false}}),false);
  assert.equal(ready({...unit,telemetry:{visibility:'hidden',immersive:true}}),false);
});
test('live VR and a fresh app image complete the observed setup checks',()=>{
  assert.equal(ready(unit),true);
  assert.equal(ready(unit,true,true,116),false);
  assert.equal(ready({...unit,preview_at:0},true,false),true);
  assert.equal(ready(unit,false),false);
  assert.equal(ready({...unit,enabled:false}),false);
});
test('slot suggestions stay at the selected site and never replace paired devices',()=>{
  const slots=[{id:'a1',site:'site-a',paired:true,enabled:true},{id:'a2',site:'site-a',paired:false,enabled:false},{id:'a3',site:'site-a',paired:false,enabled:true},{id:'m1',site:'site-b',paired:false,enabled:true}];
  assert.deepEqual(freeSlots(slots,'site-a').map(u=>u.id),['a3']);
  assert.deepEqual(freeSlots(slots,'site-b').map(u=>u.id),['m1']);
  assert.deepEqual(siteChoices(slots),[{id:'site-a',label:'Site 1'},{id:'site-b',label:'Site 2'}]);
});
