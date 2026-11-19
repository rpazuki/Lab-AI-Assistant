"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV = [
  { href: "/ask", label: "Ask" },
  { href: "/collections", label: "Collections" },
  { href: "/review", label: "Review" },
  { href: "/releases", label: "Releases" },
  { href: "/analytics", label: "Analytics" },
];

/**
 * The application bar: Flatly's midnight-blue navbar in Tabler's 3.5rem shape,
 * with the current section marked by Tabler's underline in Flatly's accent.
 *
 * It is a client component only to read the current path. The active item also
 * carries `aria-current="page"`, so the marking is not colour alone.
 */
export function NavBar() {
  // usePathname is relative to the basePath, so it compares with the hrefs as written.
  const pathname = usePathname() ?? "";
  return (
    <header className="bg-primary text-on-primary">
      <div className="mx-auto flex h-14 max-w-7xl items-center gap-8 px-4 sm:px-6">
        <Link
          href="/ask"
          className="flex shrink-0 items-center gap-2 font-semibold tracking-wide text-on-primary"
        >
          <span aria-hidden className="h-5 w-5 rounded-badge bg-accent" />
          Lab AI Assistant
        </Link>
        <nav aria-label="Main" className="-mb-px flex h-full gap-1 overflow-x-auto text-sm">
          {NAV.map((item) => {
            const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={`flex items-center border-b-2 px-3 transition-colors ${
                  active
                    ? "border-accent font-semibold text-on-primary"
                    : "border-transparent text-nav-idle hover:text-accent"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
        <span className="ml-auto hidden shrink-0 rounded-badge border border-nav-idle/40 px-2 py-0.5 text-xs text-nav-idle sm:inline">
          internal use only
        </span>
      </div>
    </header>
  );
}
