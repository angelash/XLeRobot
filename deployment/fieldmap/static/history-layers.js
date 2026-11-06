// A separate display frame for unregistered schematic obstacles. These display
// coordinates never become metric room positions or alter odometry samples.
export function obstacleLayout(data, kind, hidden) {
  const items=[];
  for(const f of data?.features||[]) {
    const category=kind(f);
    if(hidden.has(category))continue;
    const x=Number.isFinite(f.x)?f.x:0,y=Number.isFinite(f.y)?f.y:0;
    const w=Number.isFinite(f.w)&&f.w>0?f.w:50,h=Number.isFinite(f.h)&&f.h>0?f.h:40;
    const polygon=f.polygon||[[x-w/2,y-h/2],[x+w/2,y-h/2],[x+w/2,y+h/2],[x-w/2,y+h/2]];
    if(!Array.isArray(polygon)||polygon.length<3||!polygon.every(p=>Array.isArray(p)&&p.length>=2&&Number.isFinite(p[0])&&Number.isFinite(p[1])))continue;
    const label=Array.isArray(f.label_position)&&f.label_position.length>=2&&f.label_position.every(Number.isFinite)?f.label_position:[x,y];
    items.push({feature:f,kind:category,polygon,label});
  }
  const points=items.flatMap(item=>item.polygon);
  const bounds=points.length?{left:Math.min(...points.map(p=>p[0])),right:Math.max(...points.map(p=>p[0])),
    top:Math.min(...points.map(p=>p[1])),bottom:Math.max(...points.map(p=>p[1]))}:{left:-100,right:100,top:-100,bottom:100};
  const cx=(bounds.left+bounds.right)/2,cy=(bounds.top+bounds.bottom)/2;
  const unit=Math.max(100,bounds.right-bounds.left,bounds.bottom-bounds.top);
  return {items,normalize:p=>[(p[0]-cx)/unit,(p[1]-cy)/unit]};
}
