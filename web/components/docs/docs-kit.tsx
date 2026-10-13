import styles from "@/app/docs/docs.module.css";
import Link from "next/link";
import type { ReactNode } from "react";

interface GuideLink { href: string; label: string }

export function GuideBreadcrumb({ title }: { title: string }) {
  return <nav className={styles.breadcrumb} aria-label="Breadcrumb">
    <ol>
      <li><Link href="/">Events</Link></li>
      <li><Link href="/docs">Docs</Link></li>
      <li><span aria-current="page">{title}</span></li>
    </ol>
  </nav>;
}

function PageContents({ links, label = true }: { links: GuideLink[]; label?: boolean }) {
  return <nav className={styles.contents} aria-label="On this page">
    {label ? <span>On this page</span> : null}
    <ul>{links.map(link => <li key={link.href}><a href={link.href}>{link.label}</a></li>)}</ul>
  </nav>;
}

/** Explicit headings keep the contents usable in the prerendered, JavaScript-free page. */
export function GuidePage({ title, intro, contents, children }: {
  title: string; intro: ReactNode; contents: GuideLink[]; children: ReactNode;
}) {
  return <div className={styles.articleLayout}>
    <article className={styles.reading}>
      <GuideBreadcrumb title={title} />
      <h1>{title}</h1>
      {intro}
      {contents.length ? <details className={styles.mobileContents}>
        <summary>On this page</summary>
        <PageContents links={contents} label={false} />
      </details> : null}
      {children}
    </article>
    {contents.length ? <aside className={styles.tocRail}>
      <PageContents links={contents} />
    </aside> : null}
  </div>;
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
