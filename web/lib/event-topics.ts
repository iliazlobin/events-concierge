const TOPIC_LABELS: Record<string, string> = {
  ai: "AI",
  arts: "Arts",
  community: "Community",
  education: "Education",
  family: "Family",
  "food-drink": "Food & drink",
  founders: "Founders",
  gaming: "Gaming",
  government: "Government",
  music: "Music",
  networking: "Networking",
  outdoors: "Outdoors",
  sports: "Sports",
  technology: "Technology",
  volleyball: "Volleyball",
  wellness: "Wellness",
  workshop: "Workshop",
};

export type EventTopicTone =
  | "amber"
  | "blue"
  | "coral"
  | "cyan"
  | "green"
  | "rose"
  | "slate"
  | "teal"
  | "violet";

const TOPIC_TONES: Record<string, EventTopicTone> = {
  ai: "cyan",
  arts: "rose",
  "board-games": "violet",
  chess: "violet",
  community: "amber",
  education: "violet",
  family: "coral",
  "food-drink": "coral",
  founders: "teal",
  gaming: "violet",
  government: "amber",
  music: "rose",
  networking: "teal",
  outdoors: "green",
  sports: "green",
  technology: "blue",
  volleyball: "green",
  wellness: "green",
  workshop: "violet",
};

export function eventTopicLabel(topic: string): string {
  return TOPIC_LABELS[topic] ?? topic
    .split("-")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function eventTopicTone(topic?: string | null): EventTopicTone {
  return TOPIC_TONES[(topic ?? "").trim().toLowerCase()] ?? "slate";
}
