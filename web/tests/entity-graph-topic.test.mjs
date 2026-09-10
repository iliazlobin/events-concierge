import assert from 'node:assert/strict';
import test from 'node:test';
import { topicEventGraph } from '../lib/topic-graph.ts';
import { deriveEntityGraphScene } from '../lib/entity-graph.ts';
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
test('topic graph navigation persists its focus in the existing history URL',()=>{
 const filters={...emptyCatalogFilters(),topics:['education']};
 const snapshot=createConsumerHistorySnapshot('entities',filters,null,'month');
 const url=new URL(consumerHistoryUrl(snapshot,'http://localhost:3001/app'),'http://localhost:3001');
 assert.equal(url.searchParams.get('topic'),'education');
 assert.equal(url.searchParams.get('view'),'entities');
});
