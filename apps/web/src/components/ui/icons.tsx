import type { ReactNode } from "react";

import type { NavIcon } from "@/lib/navigation";

/*
 * The app's small icons, drawn inline.
 *
 * Outline glyphs on a 24-unit grid with a 2-unit round stroke, the style of the
 * Tabler shell the layout follows, coloured by `currentColor` so they take the
 * Flatly token of whatever text they sit beside. Inline rather than an icon
 * package: the CSP allows nothing but 'self', a build must not need the network,
 * and a dozen paths do not justify a dependency.
 *
 * Every icon is decorative (`aria-hidden`). The control it sits in carries the
 * accessible name, so an icon never has to be the only label.
 */

export type IconName =
  | NavIcon
  | "chevron"
  | "collapse"
  | "expand"
  | "sign-in"
  | "sign-out"
  | "account";

const PATHS: Record<IconName, ReactNode> = {
  ask: (
    <>
      <path d="M8 9h8M8 13h6" />
      <path d="M18 4a3 3 0 0 1 3 3v8a3 3 0 0 1-3 3h-5l-5 3v-3H6a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3z" />
    </>
  ),
  analytics: (
    <>
      <path d="M3 13a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z" />
      <path d="M10 5a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-2a1 1 0 0 1-1-1z" />
      <path d="M16 9a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1h-3a1 1 0 0 1-1-1z" />
    </>
  ),
  collections: (
    <>
      <path d="M5 4h4v16H5zM9 4h4v16H9z" />
      <path d="M5 8h4M9 16h4" />
      <path d="M13.8 4.6l3.9-1 3.8 15.4-3.9 1z" />
    </>
  ),
  review: (
    <>
      <path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2" />
      <path d="M9 3h6v4H9z" />
      <path d="M9 14l2 2 4-4" />
    </>
  ),
  releases: (
    <>
      <path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z" />
      <path d="M12 12l8-4.5M12 12v9M12 12L4 7.5M16 5.25l-8 4.5" />
    </>
  ),
  evaluation: (
    <>
      <path d="M5.6 18.4a9 9 0 1 1 12.8 0" />
      <path d="M12 13l3.5-3.5" />
      <circle cx="12" cy="13" r="1" />
    </>
  ),
  members: (
    <>
      <circle cx="9" cy="7" r="4" />
      <path d="M3 21v-2a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v2" />
      <path d="M16 3.13a4 4 0 0 1 0 7.75M21 21v-2a4 4 0 0 0-3-3.85" />
    </>
  ),
  operations: <path d="M3 12h4l3 8 4-16 3 8h4" />,
  chevron: <path d="M6 9l6 6 6-6" />,
  collapse: (
    <>
      <rect x="4" y="4" width="16" height="16" rx="2" />
      <path d="M9 4v16M15 10l-2 2 2 2" />
    </>
  ),
  expand: (
    <>
      <rect x="4" y="4" width="16" height="16" rx="2" />
      <path d="M9 4v16M14 10l2 2-2 2" />
    </>
  ),
  "sign-in": (
    <>
      <path d="M14 8V6a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h7a2 2 0 0 0 2-2v-2" />
      <path d="M20 12H8m3-3l-3 3 3 3" />
    </>
  ),
  "sign-out": (
    <>
      <path d="M14 8V6a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h7a2 2 0 0 0 2-2v-2" />
      <path d="M9 12h12m-3-3l3 3-3 3" />
    </>
  ),
  account: (
    <>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="10" r="3" />
      <path d="M6.2 18.4A4 4 0 0 1 10 16h4a4 4 0 0 1 3.8 2.4" />
    </>
  ),
};

export function Icon({ name, className = "h-4 w-4" }: { name: IconName; className?: string }) {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`shrink-0 ${className}`}
    >
      {PATHS[name]}
    </svg>
  );
}
