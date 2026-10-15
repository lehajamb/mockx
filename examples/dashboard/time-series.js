const table=data.series[0];
if(!table) return {data:[],layout:{title:'No data in the selected simulated window'}};
const fieldValues=(field)=>field.values.toArray ? field.values.toArray() : Array.from(field.values);
const time=table.fields.find(f=>f.name==='time');
if(!time) return {data:[],layout:{title:'Waiting for timestamped data'}};
const x=fieldValues(time);
const traces=table.fields.filter(f=>f!==time).map(f=>({name:f.name,x,y:fieldValues(f),type:'scatter',mode:'lines',line:{shape:'hv',width:2}}));
return {data:traces,layout:{xaxis:{type:'date',autorange:true,title:'Simulated UTC'},yaxis:{autorange:true},margin:{t:20,l:55,r:10,b:40},legend:{orientation:'h'}}};
