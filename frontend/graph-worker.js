import {analyze,adapterPath} from './graph-core.mjs';
import {layoutGraph,routeGraph} from './graph-layout.mjs';
self.onmessage=({data})=>{try{
 let result;
 if(data.kind==='analyze')result=analyze(data.graph,data.definitions);
 else if(data.kind==='adapter')result=adapterPath(data.source,data.target,data.definitions);
 else if(data.kind==='layout')result=layoutGraph(data.graph,data.definitions);
 else if(data.kind==='routes')result=routeGraph(data.graph,data.definitions);
 else throw Error('Unknown graph operation');
 self.postMessage({id:data.id,version:data.version,result});
}catch(error){self.postMessage({id:data.id,version:data.version,error:String(error)});}};
