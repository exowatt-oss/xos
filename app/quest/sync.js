// Pure clock math shared by the Quest player and deterministic synchronization tests.
export function playback(s, now, duration=Infinity){
  if(!s)return {playing:false,position:0};
  const elapsed=s.playing?Math.max(0,now-s.anchor_at):0;
  const limit=Number.isFinite(duration)?Math.max(0,duration-0.04):Infinity;
  return {playing:Boolean(s.playing&&now>=s.anchor_at),position:Math.max(0,Math.min(limit,s.position+elapsed))};
}
export function correction(current,target){
  const drift=target-current;
  return Math.abs(drift)>0.35?{seek:target,rate:1}:{seek:null,rate:Math.abs(drift)>0.08?(drift>0?1.025:0.975):1};
}
export function clockOffset(serverTime,sentMs,receivedMs){
  return serverTime-(sentMs+receivedMs)/2000;
}
