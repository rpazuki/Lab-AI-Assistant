"use client";

import { type ReactNode, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { Icon } from "@/components/ui/icons";

const WIDTH = 256;
const GAP = 6;
const MARGIN = 8;

/**
 * A small info button that shows a short explanation when clicked.
 *
 * Click (or Enter/Space) toggles it; Escape, a click elsewhere, scrolling or moving
 * focus away closes it. Click rather than hover, so it works the same with a mouse,
 * a keyboard and a touch screen. The button's accessible name says what it
 * explains, and the open text is linked to it with `aria-describedby`.
 *
 * The text is drawn in a portal at a fixed position, so a card's `overflow-hidden`
 * or a scrolling table never clips it; it opens below the button, or above when
 * there is no room below, and stays inside the window horizontally.
 */
export function InfoTip({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [place, setPlace] = useState<{ top: number; left: number } | null>(null);
  const id = useId();
  const button = useRef<HTMLButtonElement>(null);
  const tip = useRef<HTMLSpanElement>(null);

  useLayoutEffect(() => {
    if (!open || !button.current) {
      setPlace(null);
      return;
    }
    const anchor = button.current.getBoundingClientRect();
    const height = tip.current?.offsetHeight ?? 0;
    const below = anchor.bottom + GAP;
    const top =
      below + height + MARGIN > window.innerHeight && anchor.top - GAP - height > MARGIN
        ? anchor.top - GAP - height
        : below;
    const left = Math.min(
      Math.max(MARGIN, anchor.left),
      Math.max(MARGIN, window.innerWidth - WIDTH - MARGIN),
    );
    setPlace({ top, left });
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function away(event: MouseEvent) {
      const target = event.target as Node;
      if (!button.current?.contains(target) && !tip.current?.contains(target)) setOpen(false);
    }
    function escape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        button.current?.focus();
      }
    }
    function close() {
      setOpen(false);
    }
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", escape);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", escape);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [open]);

  return (
    <>
      <button
        ref={button}
        type="button"
        aria-label={`What “${label}” means`}
        aria-expanded={open}
        aria-describedby={open ? id : undefined}
        onClick={() => setOpen((value) => !value)}
        onBlur={() => setOpen(false)}
        className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-muted hover:text-link focus-visible:text-link"
      >
        <Icon name="info" className="h-3.5 w-3.5" />
      </button>
      {open && typeof document !== "undefined"
        ? createPortal(
            <span
              ref={tip}
              id={id}
              role="tooltip"
              // Keep focus on the button, so selecting the text does not close it.
              onMouseDown={(event) => event.preventDefault()}
              style={{
                position: "fixed",
                width: WIDTH,
                top: place?.top ?? 0,
                left: place?.left ?? 0,
                visibility: place ? "visible" : "hidden",
              }}
              className="z-50 block rounded-control border border-border bg-surface px-3 py-2 text-left text-xs leading-5 text-ink shadow-card"
            >
              <span className="mb-0.5 block font-mono font-medium text-heading">{label}</span>
              {children}
            </span>,
            document.body,
          )
        : null}
    </>
  );
}
