"use client";

import Link from "next/link";
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from "react";

import { Icon } from "@/components/ui/icons";
import { InfoTip } from "@/components/ui/InfoTip";
import { describeStatus, type StatusKind } from "@/lib/statuses";

/*
 * The component layer over the design tokens in `app/globals.css`.
 *
 * Every class here names a token (`bg-primary`, `text-muted`, `rounded-control`),
 * never a raw palette step, so a palette change is made once in the token file.
 * Shapes follow Tabler: 6px controls with a hairline border and a faint shadow,
 * 8px cards with a divided header, tinted badges.
 */

const buttonBase =
  "inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-control border font-medium " +
  "shadow-control transition-colors disabled:cursor-not-allowed disabled:opacity-45";

const buttonSize = {
  md: "px-3.5 py-2 text-sm",
  sm: "px-2.5 py-1 text-xs",
};

const buttonVariant = {
  // Flatly's primary: midnight blue, darker on hover.
  primary:
    "border-primary bg-primary text-on-primary hover:enabled:border-primary-hover hover:enabled:bg-primary-hover",
  // Tabler's default button: white, bordered, ink text.
  ghost: "border-border-strong bg-surface text-ink hover:enabled:bg-surface-subtle",
  // Outlined until hovered, so a destructive action never looks like the default.
  danger:
    "border-danger-strong bg-surface text-danger-strong hover:enabled:bg-danger-strong hover:enabled:text-on-primary",
};

export function Button({
  variant = "primary",
  size = "md",
  className = "",
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: keyof typeof buttonVariant;
  size?: keyof typeof buttonSize;
}) {
  return (
    <button
      className={`${buttonBase} ${buttonSize[size]} ${buttonVariant[variant]} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}

/** A text input. `className` adds to the base styles rather than replacing them. */
export function Input({ className = "", ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={
        "w-full rounded-control border border-border-strong bg-surface px-3 py-2 text-sm text-ink " +
        "shadow-control placeholder:text-muted focus:border-primary focus-visible:outline-offset-0 " +
        className
      }
      {...props}
    />
  );
}

/** A form label with Tabler's medium weight; the control goes inside it. */
export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block space-y-1.5 text-sm">
      <span className="block font-medium text-ink">{label}</span>
      {children}
    </label>
  );
}

/**
 * A card. `flush` drops the body padding so a divided list or a table can run to
 * the card's edges, as Tabler's card-table and list-group-flush do.
 */
export function Panel({
  title,
  children,
  aside,
  flush = false,
}: {
  title: string;
  children: ReactNode;
  aside?: ReactNode;
  flush?: boolean;
}) {
  return (
    <section className="overflow-hidden rounded-card border border-border bg-surface shadow-card">
      <header className="flex min-h-14 flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-border px-5 py-3">
        <h2 className="text-card-title font-semibold text-heading">{title}</h2>
        {aside}
      </header>
      <div className={flush ? "" : "p-5"}>{children}</div>
    </section>
  );
}

/** Rows of a flush card: hairline dividers, card-edge padding. */
export const rowList = "divide-y divide-border";
export const row = "px-5 py-4";

/**
 * Tabler's page header: an uppercase pretitle over a semibold title. `help` is the
 * page's how-to guide, linked at the right so it is one click from the work.
 */
export function PageHeader({
  pretitle,
  title,
  description,
  help,
}: {
  pretitle: string;
  title: string;
  description?: string;
  help?: string;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-start justify-between gap-x-6 gap-y-2">
      <div>
        <p className="text-pretitle font-semibold uppercase tracking-wider text-muted">{pretitle}</p>
        <h1 className="text-page-title font-semibold text-heading">{title}</h1>
        {description ? <p className="mt-1 max-w-3xl text-sm text-muted">{description}</p> : null}
      </div>
      {help ? (
        <Link
          href={help}
          className="inline-flex items-center gap-1.5 rounded-control border border-border-strong bg-surface px-2.5 py-1 text-xs font-medium text-ink shadow-control hover:bg-surface-subtle"
        >
          <Icon name="help" className="h-3.5 w-3.5" />
          How-to guide
        </Link>
      ) : null}
    </div>
  );
}

const tones = {
  danger: "bg-danger-lt text-danger-strong border-danger-strong/25",
  warning: "bg-warning-lt text-warning-strong border-warning-strong/25",
  success: "bg-success-lt text-success-strong border-success-strong/25",
  info: "bg-info-lt text-info-strong border-info-strong/25",
  neutral: "bg-neutral-lt text-neutral-strong border-neutral-strong/15",
};

export type Tone = keyof typeof tones;

/**
 * A tinted message box with a coloured leading edge. The caller chooses `role`:
 * `alert` for a failure the reader must notice, `status` for an advisory.
 */
export function Alert({
  tone,
  role,
  className = "",
  children,
}: {
  tone: Tone;
  role?: "alert" | "status";
  className?: string;
  children: ReactNode;
}) {
  return (
    <p
      role={role}
      className={`rounded-control border border-l-4 px-3 py-2.5 text-sm ${tones[tone]} ${className}`}
    >
      {children}
    </p>
  );
}

/** Which tone a state reads as. Display only: no lifecycle decision reads this. */
function stateTone(state: string): Tone {
  return /fail|reject|conflict|quarantin|dead/.test(state)
    ? "danger"
    : /await|degrad|unreview|pending|draft|proposed|queued|running|suspend|ambig|unmatch/.test(state)
      ? "warning"
      : /publish|accept|succe|ready|resolved|included|valid|pass|^active$|^open$|^imported$|^matched$/.test(state)
        ? "success"
        : "neutral";
}

/** A state chip. Colour follows meaning, and the label is always the state's own
 *  name: an operator searching logs for `awaiting_user` finds the same word here.
 *
 *  `kind` names the glossary group the value belongs to (`lib/statuses.json`); the
 *  same word can mean different things in different places (`degraded` for a run
 *  and for a parse). With it, an info button beside the chip explains the value.
 *  Every chip that shows a status passes one, so the explanation is never missing
 *  from one page and present on another. */
export function StateChip({ state, kind }: { state: string; kind?: StatusKind }) {
  const chip = (
    <span
      className={`inline-flex items-center rounded-badge border px-1.5 py-0.5 font-mono text-xs font-medium ${tones[stateTone(state)]}`}
    >
      {state}
    </span>
  );
  const description = kind ? describeStatus(kind, state) : null;
  if (!description) return chip;
  return (
    <span className="inline-flex items-center gap-1">
      {chip}
      <InfoTip label={state}>{description}</InfoTip>
    </span>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-2 text-sm text-muted">{children}</p>;
}
