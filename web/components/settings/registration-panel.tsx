"use client";

import Link from "next/link";
import { useState, type ComponentType } from "react";
import { useSession } from "@/components/session-provider";
import { availableRegistrationProviders, type RegistrationProviderId } from "@/lib/registration-providers";
import { MuseConnectionPanel } from "./muse-panel";
import styles from "./muse-panel.module.css";

const PANELS: Record<RegistrationProviderId, ComponentType> = { muse: MuseConnectionPanel };

export function RegistrationPanel() {
  const { config } = useSession();
  const providers = availableRegistrationProviders(config);
  const [selected, setSelected] = useState<RegistrationProviderId>("muse");
  const provider = providers.find(item => item.id === selected) ?? providers[0];
  const Panel = provider ? PANELS[provider.id] : null;

  return <section className="settings-panel">
    <header className="settings-panel__head">
      <h1>Registrations</h1>
      <Link href={provider?.guideHref ?? "/docs/muse"} className="settings-guide-link">Setup guide <span aria-hidden="true">→</span></Link>
    </header>
    {provider && Panel ? <>
      <div className={styles.provider}>
        <label htmlFor="registration-provider">Provider</label>
        <select id="registration-provider" value={provider.id} onChange={event => {
          const next = providers.find(item => item.id === event.target.value);
          if (next) setSelected(next.id);
        }}>{providers.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select>
      </div>
      <Panel key={provider.id} />
    </> : <p className="settings-hint">Not enabled.</p>}
  </section>;
}
