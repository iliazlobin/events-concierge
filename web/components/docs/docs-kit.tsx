import styles from "@/app/docs/docs.module.css";

interface GuideLink { href: string; label: string }

export function PageContents({ links }: { links: GuideLink[] }) {
  return <nav className={styles.contents} aria-label="On this page">
    <span>On this page</span>
    <ul>{links.map(link => <li key={link.href}><a href={link.href}>{link.label}</a></li>)}</ul>
  </nav>;
}

export function RelatedGuides({ links }: { links: GuideLink[] }) {
  return <nav className={styles.related} aria-label="Continue reading">
    <span>Continue reading</span>
    {links.map(link => <a key={link.href} href={link.href}>{link.label}<span aria-hidden="true"> →</span></a>)}
  </nav>;
}

export function CodeBlock({ label, children }: { label: string; children: string }) {
  return <figure className={styles.codeBlock}>
    <figcaption>{label}</figcaption>
    <pre><code>{children}</code></pre>
  </figure>;
}

export function FlowDiagram({ id, label, steps }: {
  id: string; label: string; steps: Array<{ title: string; detail: string }>;
}) {
  return <figure className={styles.diagram} aria-labelledby={id}>
    <figcaption id={id}>{label}</figcaption>
    <ol>{steps.map((step, index) => <li key={step.title}>
      <div className={`${styles.node} ${styles[`tone${index % 4}`]}`}>
        <strong>{step.title}</strong><span>{step.detail}</span>
      </div>
      {index < steps.length - 1 ? <span className={styles.arrow} aria-hidden="true">→</span> : null}
    </li>)}</ol>
  </figure>;
}
