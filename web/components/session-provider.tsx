"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";

import {
  ApiError,
  clearSession,
  getMe,
  getUiConfig,
  readSession,
  setCsrfContract,
} from "@/lib/api";
import { clearCatalogCache } from "@/lib/catalog-cache";
import { clearEntityGraphCache } from "@/lib/entity-graph-cache";
import type { Me, UiConfig } from "@/lib/types";

type SessionStatus = "booting" | "ready" | "unauthenticated" | "failed";

interface SessionValue {
  status: SessionStatus;
  me: Me | null;
  config: UiConfig | null;
  tenantId: string | null;
  error: string | null;
  /** Adopt a server response that already carries fresh identity, without a second round trip. */
  applyMe: (next: Me) => void;
  refresh: () => void;
}

const SessionContext = createContext<SessionValue | null>(null);

/**
 * Loads the signed-in identity once for the whole settings tree.
 *
 * This cannot be a server component: in local demo the tenant is a UUID in `localStorage`, which the
 * server cannot read, and in a deployment the session is a host-prefixed cookie a server component
 * would have to forward to the API origin directly -- bypassing the same-origin proxy the
 * architecture exists to enforce. React context does not survive a document navigation either, so
 * the provider is scoped to this route tree and the catalog shell keeps its own bootstrap.
 */
export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<SessionStatus>("booting");
  const [me, setMe] = useState<Me | null>(null);
  const [config, setConfig] = useState<UiConfig | null>(null);
  const [tenantId, setTenantId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);

  const refresh = useCallback(() => setGeneration((current) => current + 1), []);
  const applyMe = useCallback((next: Me) => setMe(next), []);

  useEffect(() => {
    let cancelled = false;
    setStatus("booting");
    setError(null);

    void getUiConfig()
      .then(async (nextConfig) => {
        if (cancelled) return;
        setConfig(nextConfig);
        setCsrfContract(nextConfig.csrf_cookie_name, nextConfig.csrf_header_name);
        const localTenant = nextConfig.local_demo ? readSession() : null;
        try {
          const identity = await getMe(localTenant);
          if (cancelled) return;
          setTenantId(localTenant);
          setMe(identity);
          setStatus("ready");
        } catch (loadError) {
          if (cancelled) return;
          if (loadError instanceof ApiError && (loadError.status === 401 || loadError.status === 404)) {
            // No resolvable account. Settings has no onboarding of its own; the catalog shell owns
            // that flow, so hand the browser back rather than rendering an orphaned form here.
            if (nextConfig.local_demo) {
              clearSession();
              clearCatalogCache();
              // The entity explorer holds graph bundles, directory bundles and detail payloads —
              // event titles, venue names, registration links and people's names — in module
              // memory that outlives an unauthenticated render.
              clearEntityGraphCache();
            }
            setStatus("unauthenticated");
            return;
          }
          setError(loadError instanceof Error ? loadError.message : "Settings are unavailable.");
          setStatus("failed");
        }
      })
      .catch((configError: unknown) => {
        if (cancelled) return;
        setError(configError instanceof Error ? configError.message : "Settings are unavailable.");
        setStatus("failed");
      });

    return () => {
      cancelled = true;
    };
  }, [generation]);

  useEffect(() => {
    if (status === "unauthenticated") window.location.assign("/");
  }, [status]);

  return (
    <SessionContext.Provider
      value={{ status, me, config, tenantId, error, applyMe, refresh }}
    >
      {children}
    </SessionContext.Provider>
  );
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession must be used inside a SessionProvider");
  return value;
}
