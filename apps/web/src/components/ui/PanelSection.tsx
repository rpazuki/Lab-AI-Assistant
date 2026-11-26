import type { ReactNode } from "react";

/**
 * One named part of a list panel, so every list on a page reads the same way:
 * **Search** narrows what the list shows, **Bulk action** acts on what the search
 * found, then the list itself. What each searches and does differs by list; the
 * names and the order do not.
 */
export function PanelSection({
  title,
  hint,
  children,
}: {
  title: "Search" | "Bulk action";
  /** One line on what this part does for this list. */
  hint?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section
      aria-label={title}
      className={`space-y-3 border-b border-border px-5 py-3 ${title === "Bulk action" ? "bg-surface-subtle" : ""}`}
    >
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-heading">{title}</h3>
        {hint ? <p className="text-xs text-muted">{hint}</p> : null}
      </header>
      {children}
    </section>
  );
}
