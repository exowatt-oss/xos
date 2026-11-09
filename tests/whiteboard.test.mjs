import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
const helpers = readFileSync(new URL("../app/js/whiteboard-export.js", import.meta.url), "utf8").replace(/export /g, "");
const source = helpers + "\n" + readFileSync(new URL("../app/js/whiteboard.js", import.meta.url), "utf8")
  .replace(/^import .*;$/gm, "").replace(/export /g, "");
function run(expression, extra = {}) {
  return runInNewContext(source + '\n' + expression, {
    structuredClone, document: { addEventListener() {} }, window: { addEventListener() {} },
    setTimeout, clearTimeout, ...extra,
  });
}
const plain = value => JSON.parse(JSON.stringify(value));

test("different tablet/TV sizes map to the same board without clipping or stretching", () => {
  assert.deepEqual(plain(run(`[
    boardPoint({left:20,top:80,width:800,height:450},420,305),
    boardPoint({left:0,top:0,width:3840,height:2160},1920,1080),
    boardPoint({left:20,top:80,width:800,height:450},-20,600)]`)), [[800,450],[800,450],[-80,1040]]);
});

test("TV applies live chunks once, then reconnects from a complete board", () => {
  const result = run(`
    let b = {active:true, epoch:0, revision:1, strokes:[]};
    const event = {kind:"stroke",active:true,epoch:0,revision:2,id:"s",color:"#17212b",width:7,offset:0,points:[[1,2],[3,4]],done:false};
    b = applyBoardEvent(b,event); b = applyBoardEvent(b,event);
    b = applyBoardEvent(b,{...event,revision:3,offset:2,points:[[5,6]],done:true});
    const original = structuredClone(b);
    b = applyBoardEvent(b,{kind:"snapshot",...original});
    const undo = applyBoardEvent(b,{kind:"undo",active:true,epoch:1,revision:4});
    const clear = applyBoardEvent(b,{kind:"clear",active:true,epoch:1,revision:4});
    const exit = applyBoardEvent(b,{kind:"exit",active:false,epoch:1,revision:4});
    ({b,original,undo,clear,exit});
  `);
  assert.deepEqual(plain(result.b.strokes[0].points), [[1,2],[3,4],[5,6]]);
  assert.deepEqual(plain(result.b.strokes), plain(result.original.strokes));
  assert.equal(result.undo.strokes.length, 0);
  assert.equal(result.clear.strokes.length, 0);
  assert.equal(result.exit.active, false);
  assert.equal(result.exit.strokes.length, 1);
});

test("Pencil/finger capture batches ordered chunks and flushes on cancellation", () => {
  const handlers = {};
  const canvas = {addEventListener(name, fn) {handlers[name]=fn;},setPointerCapture() {},
    getBoundingClientRect:()=>({left:0,top:0,width:800,height:450})};
  const result = run(`
    canvas = fakeCanvas; camera=fitCamera(canvas.getBoundingClientRect(),PAGE); board = {active:true,epoch:5,revision:1,strokes:[]}; connected = true;
    drain = () => {}; scheduleDraw = () => {}; updateStatus = () => {};
    bindDrawing();
    const e = (x,y) => ({pointerId:1,pointerType:"pen",clientX:x,clientY:y,preventDefault(){}});
    handlers.pointerdown(e(10,10));
    handlers.pointermove({...e(20,20),getCoalescedEvents:()=>[]});
    flushStroke();
    handlers.pointermove({...e(30,30),getCoalescedEvents:()=>[e(25,25),e(30,30)]});
    handlers.pointercancel(e(30,30));
    ({queue,current,timer:flushTimer});
  `, { fakeCanvas:canvas, handlers, crypto:{randomUUID:()=>"stroke-test"} });
  assert.equal(result.current, null);
  assert.equal(result.timer, 0);
  assert.deepEqual(plain(result.queue.map(s=>({offset:s.offset,points:s.points,done:s.done,epoch:s.epoch}))), [
    {offset:0,points:[[20,20]],done:false,epoch:5},
    {offset:1,points:[[40,40]],done:false,epoch:5},
    {offset:2,points:[[50,50],[60,60]],done:true,epoch:5},
  ]);
});

test("starting whiteboard stays unavailable in an active or waiting meeting", () => {
  const remote = readFileSync(new URL("../app/js/views/remote.js", import.meta.url), "utf8")
    .replace(/^import .*;$/gm, "").replace(/export function /g, "function ");
  for (const meeting of [{in_call:true},{waiting:true},{}]) {
    const html = runInNewContext(remote + `
      devices=[{id:"pc",role:"roompc",online:true,native:{meeting,volume:50,meeting_providers:["meet"]}}];
      zoomInfo.atrium={}; homeView({roomId:"atrium",roomName:"Atrium",d:null,group:"atrium"});`, {
      meeting, S:{rooms:()=>[],poke(){}},members:d=>d,window:{addEventListener(){}},
      document:{addEventListener(){}},setInterval(){},esc:String,clock:()=>"12:00",
    });
    if (meeting.in_call) assert.doesNotMatch(html, /data-act="wb-start"/);
    else {
      assert.match(html, /data-act="wb-start"/);
      assert.equal(/data-act="wb-start"[^>]*disabled/.test(html), !!meeting.waiting);
    }
  }
});


test("the full viewport maps to world space and Fit preserves the drawing's aspect", () => {
  const result=run(`({point:boardPoint({left:0,top:0,width:1180,height:820},0,0),camera:fitCamera({width:820,height:1180},PAGE),bounds:drawingBounds([{width:7,color:"#17212b",points:[[-200,1200]]}]),erasedBounds:drawingBounds([{width:32,color:"#ffffff",points:[[-9000,9000]]}])})`);
  assert.ok(result.point[1]<0,"screen space above the original page remains drawable");
  assert.equal(result.camera.scale,820/1600);
  assert.ok(result.bounds.x<0 && result.bounds.height>1200);
  assert.deepEqual(plain(result.erasedBounds),{x:0,y:0,width:1600,height:900});
});

test("SVG export retains off-page ink, curved strokes, erasers and escaped titles", () => {
  const svg=run(`svgDrawing([{width:7,color:"#17212b",points:[[-20,10],[0,30],[50,10]]},{width:32,color:"#ffffff",points:[[0,20]]}],'<Review & ideas>')`);
  assert.match(svg,/&lt;Review &amp; ideas&gt;/);
  assert.match(svg,/viewBox="-/);
  assert.match(svg,/ Q0 30 25 20/);
  assert.match(svg,/<circle[^>]+fill="#ffffff"/);
  assert.doesNotMatch(svg,/<Review/);
});
