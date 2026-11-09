import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
const source=readFileSync(new URL('../app/js/whiteboard.js',import.meta.url),'utf8')
  .replace(/^import .*;$/gm,'').replace(/export /g,'');
function fixture(){
  const downloads=[],notices=[];let resolve;
  const pending=new Promise(yes=>{resolve=yes;});
  const api=runInNewContext(source+`\n
    room='atrium';board={strokes:[]};
    rasterSnapshot=()=>({width:1,height:1,toBlob:callback=>pending.then(callback)});
    panelNotice=message=>notices.push(message);
    ({export:format=>exportDrawing(format),switchRoom:()=>{room='annex';generation++;}})`,{
    document:{addEventListener(){},createElement:()=>({click(){downloads.push(this.download);},remove(){}}),body:{appendChild(){}}},
    window:{addEventListener(){}},structuredClone,Blob,TextEncoder,Date,
    URL:{createObjectURL:()=> 'blob:local-export',revokeObjectURL(){}},setTimeout(){},
    pending,notices,navigator:{clipboard:{write:()=>pending}},ClipboardItem:class{},
    imagePDF:()=>new Uint8Array([1,2,3]),
  });
  return {api,downloads,notices,resolve:()=>resolve(new Blob(['image'])),fail:()=>resolve(null)};
}
for(const format of ['png','pdf'])test(`delayed ${format} keeps its source room filename after navigation`,async()=>{
  const r=fixture(),exporting=r.api.export(format);r.api.switchRoom();r.resolve();await exporting;
  assert.equal(r.downloads.length,1);
  assert.match(r.downloads[0],new RegExp(`^atrium-canvas-.*\\.${format}$`));
  assert.deepEqual(r.notices,[],'old export must not change the new board status');
});
test('an export failure from the previous board cannot report an error on the new board',async()=>{
  const r=fixture(),exporting=r.api.export('png');r.api.switchRoom();r.fail();await exporting;
  assert.deepEqual(r.downloads,[]);assert.deepEqual(r.notices,[]);
});
test('clipboard completion from a previous board cannot change the new board status',async()=>{
  const r=fixture(),exporting=r.api.export('copy');r.api.switchRoom();r.resolve();await exporting;
  assert.deepEqual(r.notices,[]);
});
test('a current PNG download still reports completion',async()=>{
  const r=fixture(),exporting=r.api.export('png');r.resolve();await exporting;
  assert.match(r.downloads[0],/^atrium-canvas-/);assert.deepEqual(r.notices,['PNG download started.']);
});
test('a current export failure still gives recovery feedback',async()=>{
  const r=fixture(),exporting=r.api.export('png');r.fail();await exporting;
  assert.deepEqual(r.notices,['The image could not be exported']);
});
