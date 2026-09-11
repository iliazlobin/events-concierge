"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { inspectorLocation, inspectorLocationUrl, type InspectorLocationOptions } from "@/lib/admin-inspector-navigation";

/** One selected object and inspector panel, with browser history and narrow-screen focus. */
export function useAdminInspector(options: InspectorLocationOptions) {
  const { selectionParam, panelParam, defaultPanel, defaultSelection } = options;
  const panelsKey = options.panels.join("|");
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const inspectorRef = useRef<HTMLElement>(null);
  const [location, setLocation] = useState({ selection: defaultSelection ?? null, panel: defaultPanel });

  useEffect(() => {
    const read = () => setLocation(inspectorLocation(window.location.href, optionsRef.current));
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, [selectionParam, panelParam, defaultPanel, defaultSelection, panelsKey]);

  const update = useCallback((selection: string | null, panel: string, focus: boolean) => {
    const nextUrl = inspectorLocationUrl(window.location.href, optionsRef.current, selection, panel);
    if (nextUrl !== `${window.location.pathname}${window.location.search}${window.location.hash}`) {
      window.history.pushState(window.history.state, "", nextUrl);
    }
    setLocation(inspectorLocation(window.location.href, optionsRef.current));
    if (focus && selection && window.matchMedia("(max-width: 1200px)").matches) {
      window.requestAnimationFrame(() => {
        inspectorRef.current?.scrollIntoView({ block: "start", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
        inspectorRef.current?.focus({ preventScroll: true });
      });
    }
  }, []);

  return {
    ...location, inspectorRef,
    select: useCallback((key: string | null, panel = defaultPanel) => update(key, panel, true), [defaultPanel, update]),
    setPanel: useCallback((panel: string) => update(location.selection, panel, false), [location.selection, update]),
  };
}
