import assert from 'node:assert/strict';
import test from 'node:test';
import { aggregateGraphSessions } from '../lib/entity-graph-sessions.ts';

function fixture() {
 const nodes = [
  {node_id:'entity:host',node_kind:'entity',ring:0,label:'Host'},
  ...[1,2,3].map(i=>({node_id:`event:${i}`,canonical_event_id:String(i),node_kind:'event',ring:1,label:'Morning Meditation',venue_name:'Dharma Collective',city:'San Francisco',start_at:`2026-08-${10+i}T07:30:00Z`,is_past:true,topics:['wellness']})),
 ];
 return {focus_id:'entity:host',generated_at:'2026-09-08T00:00:00Z',nodes,edges:[1,2,3].map(i=>({a:'entity:host',b:`event:${i}`,kind:'mention',roles:['host'],source_labels:['Meetup San Francisco'],observed_at:`2026-08-${10+i}T00:00:00Z`})),counts:{events:3},truncated:{events:false},same_name_candidates:[]};
}
test('different dates collapse into one visual node without mutating occurrence evidence',()=>{
 const input=fixture();const before=JSON.stringify(input);const result=aggregateGraphSessions(input);
 assert.equal(result.graph.nodes.filter(n=>n.node_kind==='event').length,1);
 assert.equal(result.graph.edges.length,1);
 const head=result.representative.get('event:1');
 assert.equal(result.groups.get(head).length,3);
 assert.match(result.graph.nodes.find(n=>n.node_kind==='event').label,/3 dates/);
 assert.equal(JSON.stringify(input),before);
 assert.deepEqual(result.groups.get(head).map(n=>n.canonical_event_id),['3','2','1']);
});
test('different venues, missing anchors and different role evidence stay separate',()=>{
 for (const alter of [g=>g.nodes[1].venue_name='Other venue',g=>g.nodes[1].city=null,g=>g.edges[0].roles=['organizer']]) {
  const input=fixture();alter(input);
  assert.equal(aggregateGraphSessions(input).graph.nodes.filter(n=>n.node_kind==='event').length,2);
 }
});
test('the next upcoming session represents a mixed past/upcoming group',()=>{
 const input=fixture();input.nodes[1].is_past=false;
 const result=aggregateGraphSessions(input);
 assert.equal(result.representative.get('event:3'),'event:1');
 assert.equal(result.groups.get('event:1')[0].canonical_event_id,'1');
});
test('reordered wire nodes keep the same representative and date order',()=>{
 const a=fixture();const b=fixture();b.nodes.reverse();b.edges.reverse();
 const left=aggregateGraphSessions(a);const right=aggregateGraphSessions(b);
 assert.deepEqual([...left.groups.values()].map(g=>g.map(n=>n.node_id)),[...right.groups.values()].map(g=>g.map(n=>n.node_id)));
});
