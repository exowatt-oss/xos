// Demo files kept on the headset (its private file system), so a step plays the full-quality file
// from local storage instead of streaming it. One file downloads at a time; files no demo uses any
// more are removed. Shared by the Quest viewer and its tests.
const LIST='xos-quest-media-v1';
export function createOffline({storage,directory,fetcher,estimate,now=()=>Date.now()}){
  let saved={};
  try{saved=JSON.parse(storage.getItem(LIST)||'{}')||{};}catch(_){saved={};}
  let dir=null,busy=false,download=null,problem='',retryAt=0,kept='';
  const remember=()=>{try{storage.setItem(LIST,JSON.stringify(saved));}catch(_){}};
  async function folder(){
    if(!dir)dir=await (await directory()).getDirectoryHandle('xos-media',{create:true});
    return dir;
  }
  // Drops files no demo uses, and anything left half-written by an interrupted download.
  async function prune(keep){
    const d=await folder();
    for await(const name of d.keys())if(!keep.has(name)||!saved[name])await d.removeEntry(name).catch(()=>{});
    for(const id of Object.keys(saved))if(!keep.has(id))delete saved[id];
    remember();
  }
  async function fetchOne(file){
    const d=await folder();
    const room=await estimate().catch(()=>null);
    if(room&&file.bytes&&room.quota-room.usage<file.bytes+50e6)throw Error('Not enough space on this headset for the demo files.');
    const handle=await d.getFileHandle(file.id,{create:true});
    const out=await handle.createWritable();
    try{
      const r=await fetcher(file.url,{cache:'no-store'});
      if(!r.ok||!r.body)throw Error('Download failed ('+r.status+')');
      download={id:file.id,received:0,total:Number(r.headers.get('content-length'))||file.bytes||0};
      const reader=r.body.getReader();
      for(;;){const {done,value}=await reader.read();if(done)break;await out.write(value);download.received+=value.byteLength;}
      await out.close();
      if(download.total&&download.received!==download.total)throw Error('Download incomplete');
      saved[file.id]=download.received;remember();
    }catch(e){
      await out.abort?.().catch(()=>{});await d.removeEntry(file.id).catch(()=>{});
      throw e;
    }finally{download=null;}
  }
  return {
    enabled:true,
    cached:()=>Object.keys(saved),
    progress:()=>download&&{...download},
    error:()=>problem,
    // Call often; it does at most one thing at a time, taking files in `library` order (the current
    // scene first). `allowed` is false while a streamed video plays, so a download never takes its
    // bandwidth.
    async sync(library,allowed){
      if(busy||!Array.isArray(library))return;
      busy=true;
      try{
        const keep=new Set(library.map(f=>f.id)),signature=[...keep].sort().join();
        if(signature!==kept){await prune(keep);kept=signature;}
        if(!allowed||now()<retryAt)return;
        const next=library.find(f=>!saved[f.id]||(f.bytes&&saved[f.id]!==f.bytes));
        if(next){await fetchOne(next);problem='';}
      }catch(e){problem=String(e.message||e).slice(0,200);retryAt=now()+30000;}
      finally{busy=false;}
    },
    // A URL for the local copy of a file, or null to stream it. Revoke it when done.
    async file(asset){
      if(!saved[asset?.id])return null;
      try{
        const f=await (await (await folder()).getFileHandle(asset.id)).getFile();
        if(f.size===saved[asset.id])return f;
      }catch(_){}
      delete saved[asset.id];remember();kept='';
      return null;
    },
  };
}
