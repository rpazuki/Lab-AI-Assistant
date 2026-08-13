"use client";

import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from "react";

const base =
  "rounded-md px-3 py-2 text-sm font-medium transition disabled:opacity-50 disabled:cursor-not-allowed";

export function Button({
  variant = "primary",
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "ghost" | "danger" }) {
  const styles = {
    primary: "bg-[var(--accent)] text-white hover:opacity-90",
    ghost: "border border-neutral-300 hover:bg-neutral-100",
    danger: "border border-red-300 text-red-700 hover:bg-red-50",
  }[variant];
  return (
    <button className={`${base} ${styles}`} {...props}>
      {children}
    </button>
  );
}

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className="w-full rounded-md border border-neutral-300 px-3 py-2 text-sm focus:border-[var(--accent)] focus:outline-none"
      {...props}
    />
  );
}

export function Panel({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-4">
      <header className="mb-3 flex items-baseline justify-between gap-4">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-neutral-500">{title}</h2>
        {aside}
      </header>
      {children}
    </section>
  );
}

/** A state chip. Colour follows meaning, and the label is always the state's own
 *  name: an operator searching logs for `awaiting_user` finds the same word here. */
export function StateChip({ state }: { state: string }) {
  const tone = /fail|reject|conflict|quarantin|dead/.test(state)
    ? "bg-red-50 text-red-700 border-red-200"
    : /await|degrad|unreview|pending|draft|proposed/.test(state)
      ? "bg-amber-50 text-amber-800 border-amber-200"
      : /publish|accept|success|ready|resolved|included|valid/.test(state)
        ? "bg-emerald-50 text-emerald-700 border-emerald-200"
        : "bg-neutral-100 text-neutral-700 border-neutral-200";
  return (
    <span className={`rounded border px-2 py-0.5 font-mono text-xs ${tone}`}>{state}</span>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-sm text-neutral-500">{children}</p>;
}
