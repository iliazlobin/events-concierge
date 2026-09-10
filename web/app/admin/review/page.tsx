import { redirect } from "next/navigation";

export default async function AdminReviewPage({
  searchParams,
}: {
  searchParams: Promise<{ window?: string | string[] }>;
}): Promise<never> {
  const { window } = await searchParams;
  const params = new URLSearchParams({ tab: "pipeline" });
  if (typeof window === "string" && ["24", "168", "336", "720", "2160"].includes(window)) {
    params.set("window", window);
  }
  redirect(`/admin?${params}`);
}
