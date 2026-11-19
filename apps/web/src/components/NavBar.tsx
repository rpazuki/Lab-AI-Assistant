"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { getMe, logout } from "@/lib/api";
import { appPath } from "@/lib/config";
import { HOME_PATH, visibleNav } from "@/lib/navigation";
import type { Me } from "@/types";

/** `undefined` while asking, `null` when there is no live session. */
type Session = Me | null | undefined;

/**
 * The application bar: Flatly's midnight-blue navbar in Tabler's 3.5rem shape,
 * with the current section marked by Tabler's underline in Flatly's accent.
 *
 * It asks the API who is signed in on every navigation, so the links follow a
 * sign-in or sign-out without a reload, and offers only the sections the member's
 * capabilities open (every one, for an admin). That is presentation: the API still
 * refuses whatever a member may not do. Until the answer arrives no section is
 * shown, so a researcher never sees an admin's links flash past.
 *
 * The active item also carries `aria-current="page"`, so the marking is not
 * colour alone.
 */
export function NavBar() {
  // usePathname is relative to the basePath, so it compares with the hrefs as written.
  const pathname = usePathname() ?? "";
  const [session, setSession] = useState<Session>(undefined);

  useEffect(() => {
    let current = true;
    getMe()
      .then((me) => current && setSession(me))
      // Any refusal or failure reads as signed out: the one thing offered is signing in.
      .catch(() => current && setSession(null));
    return () => {
      current = false;
    };
  }, [pathname]);

  async function signOut() {
    await logout();
    // A full load, not a client transition: no page keeps the signed-in member's data.
    window.location.assign(appPath("/login"));
  }

  const items = session ? visibleNav(session.capabilities) : [];
  const linkClass = (active: boolean) =>
    `flex items-center border-b-2 px-3 transition-colors ${
      active
        ? "border-accent font-semibold text-on-primary"
        : "border-transparent text-nav-idle hover:text-accent"
    }`;

  return (
    <header className="bg-primary text-on-primary">
      <div className="mx-auto flex h-14 max-w-7xl items-center gap-8 px-4 sm:px-6">
        <Link
          href={session ? HOME_PATH : "/login"}
          className="flex shrink-0 items-center gap-2 font-semibold tracking-wide text-on-primary"
        >
          <span aria-hidden className="h-5 w-5 rounded-badge bg-accent" />
          Lab AI Assistant
        </Link>
        <nav aria-label="Main" className="-mb-px flex h-full gap-1 overflow-x-auto text-sm">
          {items.map((item) => {
            const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={linkClass(active)}
              >
                {item.label}
              </Link>
            );
          })}
          {session === null ? (
            <Link
              href="/login"
              aria-current={pathname === "/login" ? "page" : undefined}
              className={linkClass(pathname === "/login")}
            >
              Sign in
            </Link>
          ) : null}
        </nav>
        <div className="ml-auto flex shrink-0 items-center gap-3 text-xs">
          <span className="hidden rounded-badge border border-nav-idle/40 px-2 py-0.5 text-nav-idle sm:inline">
            internal use only
          </span>
          {session ? (
            <>
              <span className="hidden text-nav-idle md:inline" title={session.workspace.name}>
                {session.email}
                {session.role ? ` · ${session.role.replace(/_/g, " ")}` : ""}
              </span>
              <button
                type="button"
                onClick={signOut}
                className="rounded-badge border border-nav-idle/40 px-2 py-0.5 text-on-primary transition-colors hover:border-accent hover:text-accent"
              >
                Sign out
              </button>
            </>
          ) : null}
        </div>
      </div>
    </header>
  );
}
