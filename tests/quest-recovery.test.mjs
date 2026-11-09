import test from 'node:test';
import assert from 'node:assert/strict';
import {journal,recenterPose} from '../app/quest/recovery.js';

test('lost acknowledgements and reloads do not repeat recovery actions',()=>{
  const values=new Map(),storage={getItem:k=>values.get(k),setItem:(k,v)=>values.set(k,v)};
  let j=journal(storage);j.start('cmd1');j.finish('cmd1');j.start('reload');j.reloading('reload');
  j=journal(storage);j.pendingReload();
  assert.ok(j.seen('cmd1'));assert.ok(j.seen('reload'));
  assert.deepEqual(j.acks(),[{id:'cmd1',status:'applied',error:''},{id:'reload',status:'applied'}]);
  j.start('bad');j.finish('bad','failed','Could not load scene');
  assert.equal(journal(storage).acks().at(-1).status,'failed');
});
test('recenter cancels head yaw and moves the tracked head to the viewing position',()=>{
  for(const [hx,hz,hy,rx,rz,ry,distance] of [[1,2,0,0,0,0,3],[4,-2,Math.PI/2,1,2,.3,3],[-3,6,-1.4,-1,1,-.4,0]]){
    const p=recenterPose(hx,hz,hy,rx,rz,ry,distance),delta=p.yaw-ry,c=Math.cos(delta),s=Math.sin(delta);
    const x=c*(hx-rx)+s*(hz-rz)+p.x,z=-s*(hx-rx)+c*(hz-rz)+p.z;
    assert.ok(Math.abs(x)<1e-10);assert.ok(Math.abs(z-distance)<1e-10);assert.ok(Math.abs(hy+delta)<1e-10);
  }
});
