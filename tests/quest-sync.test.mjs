import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../app/quest/sync.js',import.meta.url),'utf8');
const {playback,correction,clockOffset}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
test('scheduled starts wait for the same server instant and catch up from the anchor',()=>{
  const s={playing:true,position:12,anchor_at:100};
  assert.deepEqual(playback(s,99),{playing:false,position:12});
  assert.deepEqual(playback(s,104),{playing:true,position:16});
  assert.deepEqual(playback({...s,playing:false},500),{playing:false,position:12});
  assert.equal(playback(s,1000,40).position,39.96);
});
test('network midpoint compensates for device clock differences',()=>{
  assert.equal(clockOffset(100,1000,3000),98);
  assert.equal(playback({playing:true,position:0,anchor_at:100},4+98).position,2);
});
test('large video drift seeks once and small drift uses bounded rate correction',()=>{
  assert.deepEqual(correction(1,4),{seek:4,rate:1});
  assert.equal(correction(1,1.2).rate,1.025);
  assert.equal(correction(1.2,1).rate,.975);
  assert.equal(correction(1,1.04).rate,1);
});
