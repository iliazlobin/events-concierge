import assert from 'node:assert/strict';
import test from 'node:test';
import { groupEventSessions, discoveryLabels } from '../lib/event-discovery.ts';

const event = (id, extra = {}) => ({canonical_event_id:id,title:'Story time',organizer_name:'Library',venue_name:'Main branch',city:'village',description:'Read together',price_status:'free',source_keys:['library-main'],sources:[],...extra});
test('matching sessions collapse without losing event identity; other venues and descriptions stay separate',()=>{
  const events=[event('a'),event('b'),event('c',{venue_name:'East branch'}),event('d',{description:'Author visit'})];
  const groups=groupEventSessions(events);
  assert.equal(groups.length,3);
  assert.deepEqual(groups[0].map(e=>e.canonical_event_id),['a','b']);
});
test('unknown anchors do not collapse distinct events',()=>{
 assert.equal(groupEventSessions([event('a',{organizer_name:null}),event('b',{organizer_name:null})]).length,2);
});
test('history, ongoing and stale observations have honest labels',()=>{
 assert.deepEqual(discoveryLabels(event('a',{discovery_state:'past',source_freshness:'stale'})),['Past event']);
 assert.deepEqual(discoveryLabels(event('b',{discovery_state:'ongoing',source_freshness:'stale'})),['Ongoing','Source not checked in 7 days']);
 assert.ok(!discoveryLabels(event('c',{source_freshness:'recent'})).includes('Verified'));
});

import { variedEventChoices } from '../lib/event-discovery.ts';
test('broad result choices diversify without imposing quotas on focused requests',()=>{
 const rows=[0,1,2,3].map(i=>event(String(i),{title:`Library activity ${i}`,topics:['family']}));
 const walk=event('walk',{title:'Walk',organizer_name:'Walk club',source_keys:['walks'],topics:['outdoors']});
 const all=[...rows,walk];
 assert.equal(variedEventChoices(all,false),all);
 assert.ok(variedEventChoices(all,true).findIndex(e=>e.canonical_event_id==='walk')<4);
});
