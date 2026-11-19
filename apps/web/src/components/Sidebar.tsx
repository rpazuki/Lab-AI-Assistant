"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { Icon } from "@/components/ui/icons";
import { getMe, logout } from "@/lib/api";
import { appPath } from "@/lib/config";
import { HOME_PATH, isActive, visibleGroups } from "@/lib/navigation";
import type { Me } from "@/types";

/** `undefined` while asking, `null` when there is no live session. */
type Session = Me | null | undefined;

/** Browser storage keys. Layout preference only: nothing here is a secret. */
export const COLLAPSED_KEY = "lab.sidebar.collapsed";
export const CLOSED_GROUPS_KEY = "lab.sidebar.closed-groups";

/**
 * Storage can be missing or refuse access (a locked-down profile, a private
 * window). A preference that cannot be kept is simply not kept: the sidebar
 * still works, it just forgets.
 */
function remembered(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function remember(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Not kept; see `remembered`.
  }
}

function readClosedGroups(): string[] {
  try {
    const stored = JSON.parse(remembered(CLOSED_GROUPS_KEY) ?? "[]");
    return Array.isArray(stored) ? stored.filter((id) => typeof id === "string") : [];
  } catch {
    return [];
  }
}

/**
 * The application sidebar: Flatly's midnight blue, down the left edge.
 *
 * Sections are grouped into accordion panels, each opened and closed by its own
 * header. The whole bar can be hidden down to a rail of icons with the button at
 * its top, which is the room a long review table or a chart wants. Both choices
 * are remembered in this browser.
 *
 * It asks the API who is signed in on every navigation, so the links follow a
 * sign-in or sign-out without a reload, and offers only the sections the member's
 * capabilities open (every one, for an admin). That is presentation: the API still
 * refuses whatever a member may not do. Until the answer arrives no section is
 * shown, so a researcher never sees an admin's links flash past.
 *
 * The current page carries `aria-current="page"` as well as its highlight, and in
 * the rail each icon keeps its section's name for assistive technology and as a
 * tooltip, so nothing is marked by colour or by a glyph alone.
 */
export function Sidebar() {
  // usePathname is relative to the basePath, so it compares with the hrefs as written.
  const pathname = usePathname() ?? "";
  const [session, setSession] = useState<Session>(undefined);
  const [collapsed, setCollapsed] = useState(false);
  const [closed, setClosed] = useState<string[]>([]);

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

  // Read after mount, not during render: the server has no storage, and a first
  // render that differed from the server's would be a hydration mismatch.
  useEffect(() => {
    setCollapsed(remembered(COLLAPSED_KEY) === "1");
    setClosed(readClosedGroups());
  }, []);

  function toggleCollapsed() {
    const next = !collapsed;
    setCollapsed(next);
    remember(COLLAPSED_KEY, next ? "1" : "0");
  }

  function toggleGroup(id: string) {
    const next = closed.includes(id) ? closed.filter((item) => item !== id) : [...closed, id];
    setClosed(next);
    remember(CLOSED_GROUPS_KEY, JSON.stringify(next));
  }

  async function signOut() {
    await logout();
    // A full load, not a client transition: no page keeps the signed-in member's data.
    window.location.assign(appPath("/login"));
  }

  const groups = session ? visibleGroups(session.capabilities) : [];

  const itemClass = (active: boolean) =>
    `flex items-center gap-3 rounded-control border-l-2 py-2 text-sm transition-colors ${
      collapsed ? "justify-center px-0" : "px-3"
    } ${
      active
        ? "border-accent bg-nav-active font-semibold text-on-primary"
        : "border-transparent text-nav-idle hover:bg-nav-hover hover:text-on-primary"
    }`;

  return (
    <aside
      id="app-sidebar"
      className={`sticky top-0 flex h-screen shrink-0 flex-col bg-primary text-on-primary transition-[width] duration-200 ${
        collapsed ? "w-16" : "w-60"
      }`}
    >
      <div
        className={`flex h-14 shrink-0 items-center border-b border-nav-rule ${
          collapsed ? "justify-center" : "justify-between gap-2 pl-4 pr-2"
        }`}
      >
        {collapsed ? null : (
          <Link
            href={session ? HOME_PATH : "/login"}
            className="flex min-w-0 items-center gap-2 font-semibold tracking-wide text-on-primary"
          >
            <span aria-hidden className="h-5 w-5 shrink-0 rounded-badge bg-accent" />
            <span className="truncate">Lab AI Assistant</span>
          </Link>
        )}
        <button
          type="button"
          onClick={toggleCollapsed}
          aria-controls="app-sidebar"
          aria-expanded={!collapsed}
          aria-label={collapsed ? "Show navigation" : "Hide navigation"}
          title={collapsed ? "Show navigation" : "Hide navigation"}
          className="rounded-control p-2 text-nav-idle transition-colors hover:bg-nav-hover hover:text-on-primary"
        >
          <Icon name={collapsed ? "expand" : "collapse"} />
        </button>
      </div>

      <nav aria-label="Main" className="flex-1 overflow-y-auto px-2 py-3">
        {groups.map((group, index) => {
          const open = collapsed || !closed.includes(group.id);
          const panelId = `nav-group-${group.id}`;
          return (
            <div key={group.id} className={index > 0 ? "mt-3" : ""}>
              {collapsed ? (
                index > 0 ? <hr className="mx-2 mb-3 border-nav-rule" /> : null
              ) : (
                <button
                  type="button"
                  onClick={() => toggleGroup(group.id)}
                  aria-expanded={open}
                  aria-controls={panelId}
                  className="flex w-full items-center justify-between rounded-control px-3 py-1.5 text-pretitle font-semibold uppercase tracking-wider text-nav-idle transition-colors hover:text-on-primary"
                >
                  {group.label}
                  <Icon
                    name="chevron"
                    className={`h-3.5 w-3.5 transition-transform ${open ? "" : "-rotate-90"}`}
                  />
                </button>
              )}
              {open ? (
                <ul id={panelId} className="mt-1 space-y-0.5">
                  {group.items.map((item) => {
                    const active = isActive(pathname, item.href);
                    return (
                      <li key={item.href}>
                        <Link
                          href={item.href}
                          aria-current={active ? "page" : undefined}
                          title={collapsed ? item.label : undefined}
                          className={itemClass(active)}
                        >
                          <Icon name={item.icon} />
                          <span className={collapsed ? "sr-only" : ""}>{item.label}</span>
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              ) : null}
            </div>
          );
        })}
        {session === null ? (
          <Link
            href="/login"
            aria-current={pathname === "/login" ? "page" : undefined}
            title={collapsed ? "Sign in" : undefined}
            className={itemClass(pathname === "/login")}
          >
            <Icon name="sign-in" />
            <span className={collapsed ? "sr-only" : ""}>Sign in</span>
          </Link>
        ) : null}
      </nav>

      <div className="shrink-0 space-y-2 border-t border-nav-rule px-2 py-3 text-xs">
        {collapsed ? null : (
          <span className="mx-3 inline-block rounded-badge border border-nav-idle/40 px-2 py-0.5 text-nav-idle">
            internal use only
          </span>
        )}
        {session ? (
          <>
            <div
              className={`flex items-center gap-3 py-1 text-nav-idle ${collapsed ? "justify-center" : "px-3"}`}
              title={collapsed ? session.email : session.workspace.name}
            >
              <Icon name="account" />
              {collapsed ? (
                <span className="sr-only">{session.email}</span>
              ) : (
                <span className="min-w-0">
                  <span className="block truncate text-on-primary">{session.email}</span>
                  {session.role ? (
                    <span className="block truncate">{session.role.replace(/_/g, " ")}</span>
                  ) : null}
                </span>
              )}
            </div>
            <button
              type="button"
              onClick={signOut}
              title={collapsed ? "Sign out" : undefined}
              className={`${itemClass(false)} w-full`}
            >
              <Icon name="sign-out" />
              <span className={collapsed ? "sr-only" : ""}>Sign out</span>
            </button>
          </>
        ) : null}
      </div>
    </aside>
  );
}
