"use client";

import { useMemo, useState } from "react";
import { EntityGraphCanvas } from "@/components/entity-graph-canvas";
import { EventCard } from "@/components/event-card";
import { deriveEntityGraphScene } from "@/lib/entity-graph";
import { layoutEgoRings } from "@/lib/entity-graph-layout";
import { topicEventGraph } from "@/lib/topic-graph";
import { eventTopicLabel } from "@/lib/event-topics";
import type { EventEntityReference, EventItem } from "@/lib/types";
import "@/app/entity-graph.css";

export function TopicGraphView({topic,events,hasMore,loading,error,onLoadMore,onEntitySelect,onTopicSelect}: {
  topic:string;events:EventItem[];hasMore:boolean;loading:boolean;error:string|null;
  onLoadMore:()=>void;onEntitySelect:(reference:EventEntityReference)=>void;onTopicSelect:(topic:string)=>void;
}) {
  const [selected,setSelected]=useState<string|null>(null);
  const scene=useMemo(()=>layoutEgoRings(deriveEntityGraphScene(topicEventGraph(topic,events,hasMore)),{width:960,height:640}),[topic,events,hasMore]);
  const event=events.find(e=>`event:${e.canonical_event_id}`===selected);
  return <section className="workspace entities-view">
    <header className="workspace-heading"><div><p>TOPIC GRAPH</p><h1>{eventTopicLabel(topic)}</h1></div></header>
    <p>{events.length} loaded events matching your filters{hasMore ? " · more available" : ""}. Select an event to see its details.</p>
    {error ? <p role="alert">{error}</p> : null}
    <div className="entity-graph-stage" data-mode="graph">
      <div className="entity-graph-column">
        <EntityGraphCanvas scene={scene} selectedNodeId={selected} hoveredNodeId={null} onSelect={setSelected} onFocus={setSelected}/>
      </div>
      <aside className="entity-graph-inspector" aria-label="Topic event details">
        {event ? <EventCard event={event} expanded onToggle={()=>setSelected(null)} onEntitySelect={onEntitySelect} onTopicSelect={onTopicSelect}/> : <>
          <h2>{eventTopicLabel(topic)} events</h2>
          <ul className="entity-graph-inspector__mentions">{events.map(e=><li key={e.canonical_event_id}><button type="button" onClick={()=>setSelected(`event:${e.canonical_event_id}`)}><strong>{e.title}</strong><small>{new Date(e.start_at).toLocaleString()}</small></button></li>)}</ul>
        </>}
      </aside>
    </div>
    {hasMore ? <button className="load-more" disabled={loading} onClick={onLoadMore}>{loading ? "Loading" : "More events"}</button> : null}
  </section>;
}
