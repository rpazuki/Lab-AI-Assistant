/**
 * Which sections the navigation offers, and to whom.
 *
 * Visibility only. The API enforces every capability on every request; hiding a
 * link a member cannot use is a courtesy, not a control, so nothing here decides
 * what anyone may do. The capability names are the API's (`/auth/me`), and an
 * item shows when the member holds any one of them. Admins hold every
 * capability, so they see every section.
 */

export interface NavItem {
  href: string;
  label: string;
  anyOf: readonly string[];
}

export const NAV: readonly NavItem[] = [
  { href: "/ask", label: "Ask", anyOf: ["ask"] },
  {
    href: "/collections",
    label: "Collections",
    anyOf: ["manage_scope", "run_discovery", "decide_identity", "decide_rights", "acquire"],
  },
  { href: "/review", label: "Review", anyOf: ["review_claims", "review_parse"] },
  {
    href: "/releases",
    label: "Releases",
    anyOf: ["manage_release", "approve_release", "export"],
  },
  { href: "/evaluation", label: "Evaluation", anyOf: ["run_evaluation"] },
  { href: "/analytics", label: "Analytics", anyOf: ["view_analytics"] },
];

export function visibleNav(capabilities: readonly string[]): NavItem[] {
  const held = new Set(capabilities);
  return NAV.filter((item) => item.anyOf.some((capability) => held.has(capability)));
}

/** Paths reachable without a session. Everything else sends a visitor to sign in. */
export const PUBLIC_PATHS: readonly string[] = ["/login"];

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
