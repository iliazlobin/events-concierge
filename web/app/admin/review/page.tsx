import type { Metadata } from "next";

import { ReviewConsole } from "@/components/admin/review-console";

import "./review.css";

export const metadata: Metadata = {
  title: "Console Redesign Review · Events Concierge",
};

export default function AdminReviewPage(): React.JSX.Element {
  return <ReviewConsole />;
}
