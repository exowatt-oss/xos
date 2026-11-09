// The same world coordinates feed the full-screen viewport and every export format.
export const PAGE = {x:0,y:0,width:1600,height:900};
export function drawingBounds(strokes = []) {
  let left=0, top=0, right=1600, bottom=900;
  for (const s of strokes.filter(s=>s.color!=="#ffffff")) for (const [x,y] of s.points) {
    const margin=s.width/2+12;
    left=Math.min(left,x-margin); top=Math.min(top,y-margin);
    right=Math.max(right,x+margin); bottom=Math.max(bottom,y+margin);
  }
  return {x:left,y:top,width:right-left,height:bottom-top};
}
export function viewport(rect, camera) {
  return {scale:camera.scale, x:rect.width/2-camera.x*camera.scale,y:rect.height/2-camera.y*camera.scale};
}
export function fitCamera(rect, bounds) {
  return {x:bounds.x+bounds.width/2,y:bounds.y+bounds.height/2,
    scale:Math.min(rect.width/bounds.width,rect.height/bounds.height)};
}
const xml = text => String(text).replace(/[<>&"']/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&apos;'}[c]));
export function strokePath(points) {
  if (!points.length) return '';
  let d=`M${points[0].join(' ')}`;
  for (let i=1;i<points.length-1;i++) {
    const p=points[i],n=points[i+1]; d+=` Q${p.join(' ')} ${(p[0]+n[0])/2} ${(p[1]+n[1])/2}`;
  }
  return d+` L${points.at(-1).join(' ')}`;
}
export function svgDrawing(strokes, title='Whiteboard') {
  const b=drawingBounds(strokes);
  const paths=strokes.filter(s=>s.points.length).map(s=>s.points.length===1
    ? `<circle cx="${s.points[0][0]}" cy="${s.points[0][1]}" r="${s.width/2}" fill="${xml(s.color)}"/>`
    : `<path d="${strokePath(s.points)}" fill="none" stroke="${xml(s.color)}" stroke-width="${s.width}" stroke-linecap="round" stroke-linejoin="round"/>`).join('');
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${b.width}" height="${b.height}" viewBox="${b.x} ${b.y} ${b.width} ${b.height}"><title>${xml(title)}</title><rect x="${b.x}" y="${b.y}" width="${b.width}" height="${b.height}" fill="white"/>${paths}</svg>`;
}

// A one-page PDF with a JPEG image. Byte offsets are calculated over bytes, not JS strings.
export function imagePDF(jpeg, width, height) {
  const encode=s=>new TextEncoder().encode(s), parts=[], offsets=[0]; let length=0;
  const add=data=>{parts.push(data);length+=data.length;};
  const text=s=>add(encode(s));
  const object=(id,body)=>{offsets[id]=length;text(`${id} 0 obj\n${body}\nendobj\n`);};
  const w=842*width/Math.max(width,height),h=842*height/Math.max(width,height);
  text('%PDF-1.4\n');
  object(1,'<< /Type /Catalog /Pages 2 0 R >>');
  object(2,'<< /Type /Pages /Kids [3 0 R] /Count 1 >>');
  object(3,`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${w} ${h}] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>`);
  offsets[4]=length;text(`4 0 obj\n<< /Type /XObject /Subtype /Image /Width ${width} /Height ${height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${jpeg.length} >>\nstream\n`);
  add(jpeg);text('\nendstream\nendobj\n');
  const commands=`q ${w} 0 0 ${h} 0 0 cm /Im0 Do Q`;
  object(5,`<< /Length ${encode(commands).length} >>\nstream\n${commands}\nendstream`);
  const xref=length;
  text(`xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map(n=>String(n).padStart(10,'0')+' 00000 n \n').join('')}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`);
  const out=new Uint8Array(length);let at=0;for(const p of parts){out.set(p,at);at+=p.length;}return out;
}
