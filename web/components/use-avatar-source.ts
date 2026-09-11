"use client";

import { useEffect, useState } from "react";

/** Local demo identity is an explicit header, which an image element cannot send. */
export function useAvatarSource(url: string | null, tenantId: string | null): string | null {
  const [loaded, setLoaded] = useState<{ url: string; tenantId: string; source: string } | null>(null);

  useEffect(() => {
    if (!url || !tenantId || !/^\/v1\/me\/avatar(?:\?[^#]*)?$/.test(url)) return;
    const controller = new AbortController();
    let cancelled = false;
    void fetch(url, {
      headers: { "X-EC-Tenant-ID": tenantId },
      credentials: "same-origin",
      signal: controller.signal,
    }).then(async (response) => {
      if (!response.ok) return;
      const blob = await response.blob();
      if (!/^image\/(?:png|jpeg|webp)$/.test(blob.type) || blob.size > 32 * 1024) return;
      const source = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(blob);
      });
      if (!cancelled) setLoaded({ url, tenantId, source });
    }).catch(() => {
      // Keep the initials fallback if the account or image disappears while loading.
    });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [url, tenantId]);

  // Deployment sessions use the browser's same-origin cookie directly. Never render a previous
  // local identity's pixels while its replacement request is still loading.
  if (!tenantId) return url;
  return loaded?.url === url && loaded.tenantId === tenantId ? loaded.source : null;
}
