/**
 * Which sections the navigation offers, and to whom.
 *
 * Visibility only. The API enforces every capability on every request; hiding a
 * link a member cannot use is a courtesy, not a control, so nothing here decides
 * what anyone may do. The capability names are the API's (`/auth/me`), and an
 * item shows when the member holds any one of them. Admins hold every
 * capability, so they see every section.
 */

export type NavIcon =
  | "ask"
  | "analytics"
  | "collections"
  | "review"
  | "releases"
  | "evaluation"
  | "datasheets"
  | "answer-reviews"
  | "members"
  | "operations";

export interface NavItem {
  href: string;
  label: string;
  icon: NavIcon;
  anyOf: readonly string[];
}

/** A section of the sidebar: one accordion panel. */
export interface NavGroup {
  id: string;
  label: string;
  items: readonly NavItem[];
}

export const NAV_GROUPS: readonly NavGroup[] = [
  {
    id: "research",
    label: "Research",
    items: [
      { href: "/ask", label: "Ask", icon: "ask", anyOf: ["ask"] },
      { href: "/analytics", label: "Analytics", icon: "analytics", anyOf: ["view_analytics"] },
    ],
  },
  {
    id: "curation",
    label: "Curation",
    items: [
      {
        href: "/collections",
        label: "Collections",
        icon: "collections",
        anyOf: ["manage_scope", "run_discovery", "decide_identity", "decide_rights", "acquire"],
      },
      { href: "/review", label: "Review", icon: "review", anyOf: ["review_claims", "review_parse"] },
      {
        href: "/datasheets",
        label: "Datasheets",
        icon: "datasheets",
        anyOf: ["run_extraction"],
      },
      {
        href: "/answer-reviews",
        label: "Answer reviews",
        icon: "answer-reviews",
        anyOf: ["review_claims"],
      },
    ],
  },
  {
    id: "publishing",
    label: "Publishing",
    items: [
      {
        href: "/releases",
        label: "Releases",
        icon: "releases",
        anyOf: ["manage_release", "approve_release", "export"],
      },
      { href: "/evaluation", label: "Evaluation", icon: "evaluation", anyOf: ["run_evaluation"] },
    ],
  },
  {
    id: "administration",
    label: "Administration",
    items: [
      { href: "/members", label: "Members", icon: "members", anyOf: ["manage_members"] },
      { href: "/operations", label: "Operations", icon: "operations", anyOf: ["view_operations"] },
    ],
  },
];

/** Every item, in sidebar order. */
export const NAV: readonly NavItem[] = NAV_GROUPS.flatMap((group) => group.items);

function holds(item: NavItem, held: ReadonlySet<string>): boolean {
  return item.anyOf.some((capability) => held.has(capability));
}

export function visibleNav(capabilities: readonly string[]): NavItem[] {
  const held = new Set(capabilities);
  return NAV.filter((item) => holds(item, held));
}

/** The groups with at least one item this member may open, and only those items. */
export function visibleGroups(capabilities: readonly string[]): NavGroup[] {
  const held = new Set(capabilities);
  return NAV_GROUPS.map((group) => ({
    ...group,
    items: group.items.filter((item) => holds(item, held)),
  })).filter((group) => group.items.length > 0);
}

export function isActive(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

/** Paths reachable without a session. Everything else sends a visitor to sign in. */
export const PUBLIC_PATHS: readonly string[] = ["/login", "/invite"];

/** Where a signed-in member lands when no other destination was asked for. */
export const HOME_PATH = "/ask";

/**
 * The `next` destination after sign-in, only if it stays inside this app.
 *
 * A path such as `//evil.example` or `/\evil.example` is protocol-relative to a
 * browser, so anything but a single leading slash is refused: an open redirect on
 * the sign-in page is a phishing aid.
 */
export function safeNext(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.includes("\\")) {
    return HOME_PATH;
  }
  if (PUBLIC_PATHS.some((path) => next === path || next.startsWith(`${path}?`))) {
    return HOME_PATH;
  }
  return next;
}
