import assert from 'node:assert/strict';
import test from 'node:test';
import { topicEventGraph } from '../lib/topic-graph.ts';
import { deriveEntityGraphDetails, deriveEntityGraphScene } from '../lib/entity-graph.ts';
import { createConsumerHistorySnapshot,consumerHistoryUrl } from '../lib/consumer-history.ts';
import { emptyCatalogFilters } from '../lib/catalog-filters.ts';

test('topic graph connects only matching events and does not invent entity identities',()=>{
 const event={canonical_event_id:'one',title:'Lecture',topics:['education'],start_at:'2026-09-09T12:00:00Z',end_at:null,registration_urls:[],sources:[]};
 const graph=topicEventGraph('education',[event,{...event,canonical_event_id:'two',topics:['music']}],true);
 assert.equal(graph.nodes.length,2);
 assert.equal(graph.focus_id,'topic:education');
 assert.ok(graph.edges.every(e=>e.kind==='topic'));
 assert.equal(graph.truncated.events,true);
 const scene=deriveEntityGraphScene(graph);
 assert.deepEqual(scene.order,['topic:education','event:one']);
 assert.equal(scene.topics.length,0);
});
test('topic details retain shown occurrences and calendar evidence without entity assertions',()=>{
 const event={canonical_event_id:'upcoming',title:'Lecture',topics:['education'],start_at:'2030-06-14T12:00:00Z',end_at:null,registration_urls:['https://events.example.test/lecture'],sources:[],calendar_labels:['Campus calendar'],discovery_state:'upcoming',venue_name:'Campus',city:'Oakland'};
 const past={...event,canonical_event_id:'past',start_at:'2020-06-14T12:00:00Z',discovery_state:'past',calendar_labels:['Archive calendar']};
 const graph=topicEventGraph('education',[past,event,{...event,canonical_event_id:'unmatched',topics:['music']}],true);
 const details=deriveEntityGraphDetails(deriveEntityGraphScene(graph));
 assert.equal(details.upcoming.length,1);
 assert.equal(details.past.length,1);
 assert.deepEqual([...details.upcoming,...details.past].map(row=>row.node_id),['event:upcoming','event:past']);
 assert.deepEqual(details.upcoming[0],{node_id:'event:upcoming',title:'Lecture',start_at:event.start_at,is_past:false,venue_name:'Campus',city:'Oakland',roles:[],source_labels:['Campus calendar'],observed_at:null,registration_url:event.registration_urls[0],entity_count:1});
 assert.deepEqual(details.past[0].roles,[]);
 assert.deepEqual(details.past[0].source_labels,['Archive calendar']);
 assert.equal(details.past[0].start_at,past.start_at);
 assert.equal(details.truncated.events,true);
 assert.ok(graph.edges.every(edge=>edge.kind==='topic'));
});
test('topic graph navigation persists its focus in the existing history URL',()=>{
 const filters={...emptyCatalogFilters(),topics:['education']};
 const snapshot=createConsumerHistorySnapshot('entities',filters,null,'month');
 const url=new URL(consumerHistoryUrl(snapshot,'http://localhost:3001/app'),'http://localhost:3001');
 assert.equal(url.searchParams.get('topic'),'education');
 assert.equal(url.searchParams.get('view'),'entities');
});
