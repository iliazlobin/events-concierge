"use client";

import { LoaderCircle } from "lucide-react";
import { useEffect } from "react";

import { resolveReturnPath } from "@/lib/return-path";

export function AppLanding() {
  useEffect(() => {
    window.location.replace(resolveReturnPath(window.location.hash));
  }, []);

  return (
    <main className="app-loading">
      <span className="brand-symbol" aria-hidden="true">
        <i />
        <i />
      </span>
      <LoaderCircle className="spin" aria-hidden="true" />
    </main>
  );
}
