"use client";

import { useState } from "react";
import { museInstruction } from "@/lib/muse";
import styles from "@/app/docs/docs.module.css";

/** The same connector instruction used by the app; no account data or key is included. */
export function MuseInstructions() {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function copyInstruction() {
    setCopied(false); setError(null);
    try { await navigator.clipboard.writeText(museInstruction()); setCopied(true); }
    catch { setError("Clipboard unavailable. Select and copy the instruction below."); }
  }
  return <div className={styles.instruction}>
    {error ? <p role="alert" className="settings-error">{error}</p> : null}
    <label htmlFor="muse-signup-instruction">Muse instruction</label>
    <textarea id="muse-signup-instruction" aria-label="Muse signup instruction" readOnly value={museInstruction()} />
    <button type="button" className="button" aria-label="Copy Muse instruction" onClick={copyInstruction}>Copy instruction</button>
    <noscript><p>Select and copy the instruction above.</p></noscript>
    {copied ? <p role="status">Instruction copied.</p> : null}
  </div>;
}
