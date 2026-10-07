"use client";

import { useMemo, useState } from "react";
import { EntityGraphCanvas } from "@/components/entity-graph-canvas";
import { GraphEventCard } from "@/components/graph-event-card";
import { GraphWorkspaceHeading } from "@/components/graph-workspace-heading";
import { useGraphDetailNavigation } from "@/components/use-graph-detail-navigation";
import { deriveEntityGraphScene } from "@/lib/entity-graph";
import { layoutEgoRings } from "@/lib/entity-graph-layout";
import { topicEventGraph } from "@/lib/topic-graph";
import { eventTopicLabel } from "@/lib/event-topics";
import type { EventEntityReference, EventItem } from "@/lib/types";
import "@/app/entity-graph.css";

export function TopicGraphView({topic,events,hasMore,loading,error,onOverview,onLoadMore,onEntitySelect,onTopicSelect}: {
  topic:string;events:EventItem[];hasMore:boolean;loading:boolean;error:string|null;
  onOverview:()=>void;onLoadMore:()=>void;onEntitySelect:(reference:EventEntityReference)=>void;onTopicSelect:(topic:string)=>void;
}) {
  const [selected,setSelected]=useState<string|null>(null);
  const { stageRef, selectNode, cancelScroll } = useGraphDetailNavigation(selected, setSelected);
  const scene=useMemo(()=>layoutEgoRings(deriveEntityGraphScene(topicEventGraph(topic,events,hasMore)),{width:960,height:640}),[topic,events,hasMore]);
  const event=events.find(e=>`event:${e.canonical_event_id}`===selected);
  return <section className="workspace entities-view">
    <GraphWorkspaceHeading current={eventTopicLabel(topic)} onRoot={onOverview}
      summary={`${events.length} loaded events matching your filters${hasMore ? " · more available" : ""}`}/>
    {error ? <p role="alert">{error}</p> : null}
    <div className="entity-graph-stage" ref={stageRef} data-selection={event ? "event" : "none"}
      onDoubleClickCapture={cancelScroll}
      onKeyDownCapture={(event) => { if (event.key === "Enter" && event.shiftKey) cancelScroll(); }}>
      <div className="entity-graph-column">
        <EntityGraphCanvas scene={scene} selectedNodeId={selected} hoveredNodeId={null} onSelect={selectNode} onFocus={setSelected}/>
      </div>
      {event ? <GraphEventCard event={event} onClose={()=>selectNode(null)}
        onEntitySelect={onEntitySelect} onTopicSelect={onTopicSelect}/> : null}
    </div>
    {hasMore ? <button className="load-more" disabled={loading} onClick={onLoadMore}>{loading ? "Loading" : "More events"}</button> : null}
  </section>;
}
