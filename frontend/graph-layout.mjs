// Geometry is shared by the worker and renderer; port counts determine real card height.
export const NODE_WIDTH=207, NODE_HEADER=43, PORT_ROW=19;
export const NODE_GAP=30, COLUMN_GAP=112;
const compare=(a,b)=>String(a)<String(b)?-1:String(a)>String(b)?1:0;
const median=values=>{const sorted=[...values].sort((a,b)=>a-b),middle=sorted.length>>1;return sorted.length%2?sorted[middle]:(sorted[middle-1]+sorted[middle])/2;};
const specMap=definitions=>definitions instanceof Map?definitions:new Map(definitions.map(s=>[s.id,s]));
export function nodeHeight(node,definitions){const s=definitions.get(node.tool);return NODE_HEADER+Math.max(s?.inputs?.length||0,s?.outputs?.length||0,1)*PORT_ROW+31;}
export function portPosition(node,spec,direction,key){const ports=spec?.[direction==='output'?'outputs':'inputs']||[],index=ports.findIndex(p=>p.key===key);if(index<0)return null;return {x:node.x+(direction==='output'?NODE_WIDTH:0),y:node.y+NODE_HEADER+12+index*PORT_ROW};}
export function rectanglesOverlap(a,b,gap=0){return a.x<b.x+b.width+gap&&a.x+a.width+gap>b.x&&a.y<b.y+b.height+gap&&a.y+a.height+gap>b.y;}
const rect=(n,specs)=>({id:n.id,x:n.x,y:n.y,width:NODE_WIDTH,height:nodeHeight(n,specs)});

// The closest clear candidate makes drag/drop and keyboard movements collision-safe.
export function clearPosition(node,nodes,definitions,desired=node,gap=24){
 const specs=specMap(definitions),height=nodeHeight(node,specs),others=nodes.filter(n=>n.id!==node.id).map(n=>rect(n,specs));
 const origin={x:Number.isFinite(desired.x)?desired.x:70,y:Number.isFinite(desired.y)?desired.y:100};
 const clear=p=>!others.some(r=>rectanglesOverlap({...p,width:NODE_WIDTH,height},r,gap));
 if(clear(origin))return origin;
 const candidates=[];
 for(const r of others){
  candidates.push({x:origin.x,y:r.y-height-gap},{x:origin.x,y:r.y+r.height+gap},{x:r.x-NODE_WIDTH-gap,y:origin.y},{x:r.x+r.width+gap,y:origin.y});
 }
 candidates.sort((a,b)=>Math.hypot(a.x-origin.x,a.y-origin.y)-Math.hypot(b.x-origin.x,b.y-origin.y)||a.y-b.y||a.x-b.x);
 for(const p of candidates)if(clear(p))return p;
 return {x:origin.x,y:Math.max(origin.y,...others.map(r=>r.y+r.height+gap))};
}
export function separateNodes(graph,definitions){
 const specs=specMap(definitions),index=new ObstacleIndex([]),gap=24;
 for(const node of graph.nodes){
  node.x=Number.isFinite(node.x)?node.x:70;node.y=Number.isFinite(node.y)?node.y:100;
  const height=nodeHeight(node,specs);
  for(;;){const box={x:node.x,y:node.y,width:NODE_WIDTH,height},near=index.query({x:box.x-gap,y:box.y-gap},{x:box.x+box.width+gap,y:box.y+height+gap}),overlaps=[...near].filter(r=>rectanglesOverlap(box,r,gap));if(!overlaps.length)break;node.y=Math.max(...overlaps.map(r=>r.y+r.height+gap));}
  index.add(rect(node,specs));
 }
 return graph;
}

// Least-squares isotonic placement: keep order and measured card separation while
// aligning connected ports. PAVA is linear in the number of nodes in the layer.
function pack(layer,desired,heights){
 let offset=0;const blocks=[],offsets=[];
 for(let i=0;i<layer.length;i++){
  offsets.push(offset);blocks.push({start:i,end:i,sum:desired[i]-offset,count:1});
  while(blocks.length>1&&blocks.at(-2).sum/blocks.at(-2).count>blocks.at(-1).sum/blocks.at(-1).count){const b=blocks.pop(),a=blocks.pop();blocks.push({start:a.start,end:b.end,sum:a.sum+b.sum,count:a.count+b.count});}
  offset+=heights.get(layer[i])+NODE_GAP;
 }
 const y=new Map();for(const b of blocks)for(let i=b.start;i<=b.end;i++)y.set(layer[i],b.sum/b.count+offsets[i]);return y;
}
function componentLayout(ids,edges,nodes,specs){
 const incoming=new Map(ids.map(id=>[id,[]])),outgoing=new Map(ids.map(id=>[id,[]])),degree=new Map(ids.map(id=>[id,0])),rank=new Map(ids.map(id=>[id,0]));
 for(const e of edges){outgoing.get(e.source).push(e);incoming.get(e.target).push(e);degree.set(e.target,degree.get(e.target)+1);}
 const queue=ids.filter(id=>!degree.get(id)).sort(compare),order=[];
 for(let i=0;i<queue.length;i++){const id=queue[i];order.push(id);for(const e of outgoing.get(id)){rank.set(e.target,Math.max(rank.get(e.target),rank.get(id)+1));degree.set(e.target,degree.get(e.target)-1);if(!degree.get(e.target))queue.push(e.target);}}
 // Invalid cyclic graphs remain editable and separated; execution validation reports cycles.
 for(const id of ids)if(degree.get(id)>0)rank.set(id,0);
 const layers=[];for(const id of ids){const r=rank.get(id);(layers[r]??=[]).push(id);}for(const layer of layers)layer.sort(compare);
 const heights=new Map(ids.map(id=>[id,nodeHeight(nodes.get(id),specs)])),position=new Map();
 const updatePositions=()=>{for(const layer of layers)layer.forEach((id,i)=>position.set(id,(i+.5)/layer.length));};
 const portIndex=(id,direction,key)=>Math.max(0,(specs.get(nodes.get(id).tool)?.[direction]||[]).findIndex(p=>p.key===key));
 const cost=()=>{let total=0;for(const e of edges)total+=Math.abs(position.get(e.source)-position.get(e.target));return total;};
 updatePositions();let best=layers.map(a=>[...a]),bestCost=cost();
 // Alternating port-aware barycentric sweeps reduce crossings without changing dependencies.
 for(let pass=0;pass<10;pass++){
  const forward=pass%2===0,indices=layers.map((_,i)=>i);if(!forward)indices.reverse();
  for(const r of indices){const layer=layers[r],scores=new Map(layer.map(id=>{
   const links=(forward?incoming:outgoing).get(id),values=links.map(e=>position.get(forward?e.source:e.target)+(portIndex(forward?e.source:e.target,forward?'outputs':'inputs',forward?e.sourcePort:e.targetPort)-portIndex(id,forward?'inputs':'outputs',forward?e.targetPort:e.sourcePort))*.001);
   return [id,values.length?values.reduce((a,b)=>a+b,0)/values.length:position.get(id)];
  }));const previous=new Map(layer.map((id,i)=>[id,i]));layer.sort((a,b)=>scores.get(a)-scores.get(b)||previous.get(a)-previous.get(b)||compare(a,b));updatePositions();}
  const value=cost();if(value<=bestCost){bestCost=value;best=layers.map(a=>[...a]);}
 }
 layers.splice(0,layers.length,...best);
 let y=new Map();const totals=layers.map(layer=>layer.reduce((sum,id)=>sum+heights.get(id)+NODE_GAP,0)-NODE_GAP),maxHeight=Math.max(...totals);
 layers.forEach((layer,r)=>{let top=(maxHeight-totals[r])/2;for(const id of layer){y.set(id,top);top+=heights.get(id)+NODE_GAP;}});
 for(let pass=0;pass<6;pass++){
  const forward=pass%2===0,indices=layers.map((_,i)=>i);if(!forward)indices.reverse();
  for(const r of indices){const layer=layers[r],desired=layer.map(id=>{const links=(forward?incoming:outgoing).get(id);const values=links.filter(e=>rank.get(e.source)!==rank.get(e.target)).map(e=>{const other=forward?e.source:e.target;return y.get(other)+(portIndex(other,forward?'outputs':'inputs',forward?e.sourcePort:e.targetPort)-portIndex(id,forward?'inputs':'outputs',forward?e.targetPort:e.sourcePort))*PORT_ROW;});return values.length?median(values):y.get(id);});for(const [id,top] of pack(layer,desired,heights))y.set(id,top);}
 }
 const minY=Math.min(...y.values());let height=0;
 for(const id of ids){const node=nodes.get(id);node.x=rank.get(id)*(NODE_WIDTH+COLUMN_GAP);node.y=y.get(id)-minY;height=Math.max(height,node.y+heights.get(id));}
 return {ids,width:layers.length*NODE_WIDTH+(layers.length-1)*COLUMN_GAP,height};
}
export function layoutGraph(graph,definitions){
 const specs=specMap(definitions),nodes=new Map(graph.nodes.map(n=>[n.id,n])),adj=new Map(graph.nodes.map(n=>[n.id,[]]));
 const edges=graph.edges.filter(e=>nodes.has(e.source)&&nodes.has(e.target)).sort((a,b)=>compare(a.id,b.id));
 for(const e of edges){adj.get(e.source).push(e.target);adj.get(e.target).push(e.source);}
 const visited=new Set(),components=[];
 for(const first of [...nodes.keys()].sort(compare)){
  if(visited.has(first))continue;const ids=[first];visited.add(first);
  for(let i=0;i<ids.length;i++)for(const next of adj.get(ids[i]))if(!visited.has(next)){visited.add(next);ids.push(next);}
  ids.sort(compare);const member=new Set(ids);components.push(componentLayout(ids,edges.filter(e=>member.has(e.source)),nodes,specs));
 }
 components.sort((a,b)=>b.width*b.height-a.width*a.height||compare(a.ids[0],b.ids[0]));
 const target=Math.max(720,...components.map(c=>c.width),Math.sqrt(components.reduce((a,c)=>a+(c.width+60)*(c.height+60),0)*1.6));
 let x=70,y=100,rowHeight=0;
 for(const component of components){if(x>70&&x+component.width>70+target){x=70;y+=rowHeight+60;rowHeight=0;}for(const id of component.ids){const n=nodes.get(id);n.x=Math.round(n.x+x);n.y=Math.round(n.y+y);}x+=component.width+60;rowHeight=Math.max(rowHeight,component.height);}
 return graph;
}

class ObstacleIndex{
 constructor(rectangles){this.cells=new Map();this.rectangles=rectangles;this.cell=192;for(const r of rectangles)this.add(r);}
 add(r){for(let x=Math.floor(r.x/this.cell);x<=Math.floor((r.x+r.width)/this.cell);x++)for(let y=Math.floor(r.y/this.cell);y<=Math.floor((r.y+r.height)/this.cell);y++){const k=x+':'+y;if(!this.cells.has(k))this.cells.set(k,[]);this.cells.get(k).push(r);}}
 query(a,b){const result=new Set();for(let x=Math.floor(Math.min(a.x,b.x)/this.cell);x<=Math.floor(Math.max(a.x,b.x)/this.cell);x++)for(let y=Math.floor(Math.min(a.y,b.y)/this.cell);y<=Math.floor(Math.max(a.y,b.y)/this.cell);y++)for(const r of this.cells.get(x+':'+y)||[])result.add(r);return result;}
 clear(a,b,ignore){for(const r of this.query(a,b)){if(ignore?.has(r.id))continue;if(segmentIntersectsRect(a,b,r))return false;}return true;}
}
export function segmentIntersectsRect(a,b,r){
 const left=r.x,right=r.x+r.width,top=r.y,bottom=r.y+r.height;
 if(a.x===b.x)return a.x>left+.001&&a.x<right-.001&&Math.max(a.y,b.y)>top+.001&&Math.min(a.y,b.y)<bottom-.001;
 if(a.y===b.y)return a.y>top+.001&&a.y<bottom-.001&&Math.max(a.x,b.x)>left+.001&&Math.min(a.x,b.x)<right-.001;
 return true; // Routes are rectilinear; a diagonal is never accepted as obstacle-safe.
}
function simplify(points){const out=[];for(const p of points){if(out.length&&p.x===out.at(-1).x&&p.y===out.at(-1).y)continue;while(out.length>1&&((out.at(-2).x===out.at(-1).x&&out.at(-1).x===p.x)||(out.at(-2).y===out.at(-1).y&&out.at(-1).y===p.y)))out.pop();out.push(p);}return out;}
class Heap{constructor(){this.items=[];}push(v){const a=this.items;a.push(v);let i=a.length-1;while(i){const p=(i-1)>>1;if(a[p].f<=v.f)break;a[i]=a[p];i=p;}a[i]=v;}pop(){const a=this.items,result=a[0],v=a.pop();if(a.length){let i=0;while(i*2+1<a.length){let c=i*2+1;if(c+1<a.length&&a[c+1].f<a[c].f)c++;if(a[c].f>=v.f)break;a[i]=a[c];i=c;}a[i]=v;}return result;}get length(){return this.items.length;}}
function searchRoute(start,end,index,coordinates){
 const xs=[...new Set([...coordinates.x,start.x,end.x])].sort((a,b)=>a-b),ys=[...new Set([...coordinates.y,start.y,end.y])].sort((a,b)=>a-b),nx=xs.length;
 const sx=xs.indexOf(start.x),sy=ys.indexOf(start.y),tx=xs.indexOf(end.x),ty=ys.indexOf(end.y),heap=new Heap(),scores=new Map(),previous=new Map();
 const key=(x,y,d)=>((y*nx+x)*2+d),heuristic=(x,y)=>Math.abs(xs[x]-end.x)+Math.abs(ys[y]-end.y);
 for(let d=0;d<2;d++){const k=key(sx,sy,d);scores.set(k,0);heap.push({x:sx,y:sy,d,k,g:0,f:heuristic(sx,sy)});}
 while(heap.length){const current=heap.pop();if(current.g!==scores.get(current.k))continue;if(current.x===tx&&current.y===ty){const points=[];let k=current.k;while(k!==undefined){const p=Math.floor(k/2);points.push({x:xs[p%nx],y:ys[Math.floor(p/nx)]});k=previous.get(k);}return simplify(points.reverse());}
  for(const [dx,dy,d] of [[1,0,0],[0,1,1],[-1,0,0],[0,-1,1]]){const x=current.x+dx,y=current.y+dy;if(x<0||x>=nx||y<0||y>=ys.length)continue;const a={x:xs[current.x],y:ys[current.y]},b={x:xs[x],y:ys[y]};if(!index.clear(a,b))continue;const k=key(x,y,d),g=current.g+Math.abs(a.x-b.x)+Math.abs(a.y-b.y)+(d===current.d?0:18);if(g>=(scores.get(k)??Infinity))continue;scores.set(k,g);previous.set(k,current.k);heap.push({x,y,d,k,g,f:g+heuristic(x,y)});}
 }
 return null;
}

// Routes are computed once per graph change, off the UI thread. Direct channels
// cover adjacent ranks; longer connections use clear horizontal corridors. A*
// over obstacle boundaries handles manually arranged or backward connections.
export function routeGraph(graph,definitions){
 const specs=specMap(definitions),nodes=new Map(graph.nodes.map(n=>[n.id,n])),rectangles=graph.nodes.map(n=>rect(n,specs));
 const expanded=rectangles.map(r=>({...r,x:r.x-8,y:r.y-8,width:r.width+16,height:r.height+16})),index=new ObstacleIndex(expanded);
 const coordinates={x:expanded.flatMap(r=>[r.x-3,r.x+r.width+3]),y:expanded.flatMap(r=>[r.y-3,r.y+r.height+3])};
 const top=Math.min(0,...expanded.map(r=>r.y))-24,bottom=Math.max(0,...expanded.map(r=>r.y+r.height))+24;
 const corridors=[...new Set([...coordinates.y,top,bottom])],routes={},outCounts=new Map(),inCounts=new Map(),outOffsets=new Map(),inOffsets=new Map(),used=new Map();
 const edges=[...graph.edges].sort((a,b)=>{const an=nodes.get(a.target),bn=nodes.get(b.target);return (an?.y||0)-(bn?.y||0)||compare(a.id,b.id);});
 for(const e of edges){outCounts.set(e.source,(outCounts.get(e.source)||0)+1);inCounts.set(e.target,(inCounts.get(e.target)||0)+1);}
 for(const e of edges){
  const a=nodes.get(e.source),b=nodes.get(e.target);if(!a||!b)continue;
  const from=portPosition(a,specs.get(a.tool),'output',e.sourcePort),to=portPosition(b,specs.get(b.tool),'input',e.targetPort);if(!from||!to)continue;
  const out=outOffsets.get(a.id)||0,input=inOffsets.get(b.id)||0;outOffsets.set(a.id,out+1);inOffsets.set(b.id,input+1);
  const start={x:from.x+12,y:from.y},end={x:to.x-12,y:to.y},gap=end.x-start.x;
  const lanes=gap>0?[start.x+Math.max(0,Math.min(gap,18+(out%9)*5)),end.x-Math.max(0,Math.min(gap,18+(input%9)*5)),start.x+gap*(out+1)/(outCounts.get(a.id)+1),(start.x+end.x)/2]:[];
  const candidates=lanes.map(x=>[start,{x,y:start.y},{x,y:end.y},end]);
  const preferred=(start.y+end.y)/2,nearby=[...corridors].sort((x,y)=>Math.abs(x-preferred)-Math.abs(y-preferred)||x-y).slice(0,18);
  for(const y of [...nearby,top-(out%9)*5,bottom+(input%9)*5])candidates.push([start,{x:start.x,y},{x:end.x,y},end]);
  let chosen=null,best=Infinity;
  for(const raw of candidates){const points=simplify(raw);if(!points.slice(1).every((p,i)=>index.clear(points[i],p)))continue;let cost=points.length*12;for(let i=1;i<points.length;i++){const p=points[i-1],q=points[i],length=Math.abs(p.x-q.x)+Math.abs(p.y-q.y),segment=[p.x,p.y,q.x,q.y].join(',');cost+=length+Math.min(length,150)*(used.get(segment)||0)*2;}if(cost<best){chosen=points;best=cost;}}
  if(!chosen)chosen=searchRoute(start,end,index,coordinates);
  // A disconnected port (e.g. overlapping malformed imported cards) is reported,
  // never painted as a supposedly safe straight line through another card.
  if(!chosen){routes[e.id]={points:[],blocked:true};continue;}
  const points=simplify([from,...chosen,to]);for(let i=1;i<chosen.length;i++){const p=chosen[i-1],q=chosen[i],key=[p.x,p.y,q.x,q.y].join(',');used.set(key,(used.get(key)||0)+1);}
  routes[e.id]={points,bounds:{x:Math.min(...points.map(p=>p.x)),y:Math.min(...points.map(p=>p.y)),r:Math.max(...points.map(p=>p.x)),b:Math.max(...points.map(p=>p.y))}};
 }
 return routes;
}
