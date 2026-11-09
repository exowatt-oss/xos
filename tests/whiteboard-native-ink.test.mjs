import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
const source=readFileSync(new URL('../app/js/whiteboard.js',import.meta.url),'utf8').replace(/^import .*;$/gm,'').replace(/export /g,'');
function fixture({app=true}={}){
 const posted=[],requests=[];
 const window={addEventListener(){},...(app?{xosNative:{ink:1},webkit:{messageHandlers:{xosInk:{postMessage:m=>posted.push(structuredClone(m))}}}}:{})};
 const api=runInNewContext(source+`\nupdateOverlay=()=>{};({
 open:(b)=>{room='atrium';controller=true;connected=true;board=b;overlay={querySelector:()=>({textContent:'Live on TV',classList:{toggle(){}},setAttribute(){},style:{}}),querySelectorAll:()=>[]};canvas={style:{}};syncInk();},
 setBoard:b=>{board=b;syncInk();},sync:()=>syncInk(),queue:()=>queue,idle:()=>new Promise(r=>{const t=()=>sending||queue.length?setImmediate(t):r();t();})})`,{
  document:{addEventListener(){},querySelector:()=>null,body:{classList:{toggle(){}}}},window,location:{origin:'https://xos.test'},
  EventSource:class{static CLOSED=2;close(){}},structuredClone,setTimeout,clearTimeout,URL,setImmediate,
  controlFetch:async(path,options)=>{const op=JSON.parse(options.body);requests.push(op);
   return {ok:true,status:200,json:async()=>({kind:op.action,epoch:op.epoch,revision:requests.length,active:true,...op})};},
  fetch:async()=>({json:async()=>({})}),
 });
 return {api,posted,requests,window};
}
const board=(revision,strokes=[],active=true)=>({epoch:4,revision,active,strokes});
const ink={id:'11111111-1111-4111-8111-111111111111',color:'#17212b',width:4.5,points:[[1,2]],done:true};

test('the iPad app gets the board once, then only status until the drawing changes, then close',()=>{
 const r=fixture();r.api.open(board(1,[ink]));
 assert.equal(r.posted[0].type,'open');assert.equal(r.posted[0].room,'atrium');
 assert.equal(r.posted[0].share_url,'https://xos.test/u/whiteboards/atrium');
 assert.deepEqual(r.posted[0].board.strokes,[{id:ink.id,color:ink.color,width:4.5,points:[[1,2]]}]);
 r.api.sync();assert.equal(r.posted[1].type,'state');assert.equal(r.posted[1].board,undefined);
 r.api.setBoard(board(2));assert.deepEqual(r.posted[2].board.strokes,[]);
 r.api.setBoard(board(3,[],false));assert.equal(r.posted.at(-1).type,'close');
});

test('a browser without the app never talks to the native bridge',()=>{
 const r=fixture({app:false});r.api.open(board(1));assert.equal(r.posted.length,0);
});

test('a native Pencil stroke is sent in bounded, ordered chunks on the current epoch',async()=>{
 const r=fixture();r.api.open(board(1));
 const points=Array.from({length:300},(_,i)=>[i,i]);
 r.window.xosInk({type:'stroke',id:ink.id,color:'#f5c51866',width:20,points});
 await r.api.idle();
 assert.deepEqual(r.requests.map(op=>[op.action,op.epoch,op.offset,op.points.length,op.done]),
  [['stroke',4,0,256,false],['stroke',4,256,44,true]]);
 assert.equal(r.requests[0].color,'#f5c51866');
 const last=r.posted.at(-1);assert.equal(last.handled,1);assert.equal(last.busy,false);
});

test('native edits and Done map to replace and exit, and every message is counted',async()=>{
 const r=fixture();r.api.open(board(1,[ink]));
 r.window.xosInk({type:'replace',strokes:[]});r.window.xosInk({type:'exit'});
 assert.equal(r.posted.at(-1).busy,true,'the app waits while its edits are in flight');
 await r.api.idle();
 assert.deepEqual(r.requests.map(op=>op.action),['replace','exit']);
 assert.deepEqual(r.requests[0].strokes,[]);assert.equal(r.posted.at(-1).handled,2);
});
