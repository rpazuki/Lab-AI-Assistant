"use client";

import { type FormEvent, useEffect, useState } from "react";

import { Button, Input } from "@/components/ui/primitives";

/**
 * A search box for a list the server pages: what is typed is sent when **Search**
 * is pressed (or Enter), and the server looks through every row, not the page on
 * screen. Every word must appear; times match as they are shown (`2026-11-23 16:17`).
 */
export function ServerSearch({
  label,
  placeholder = "Words, an id, or a date such as 2026-11-23",
  query,
  onSearch,
  className = "",
}: {
  /** What the rows are, for the box's accessible name ("Search dead letters"). */
  label: string;
  placeholder?: string;
  /** The search in force, so the box shows it and offers **Clear**. */
  query: string;
  onSearch: (query: string) => void;
  className?: string;
}) {
  const [typed, setTyped] = useState(query);
  useEffect(() => setTyped(query), [query]);
  function submit(event: FormEvent) {
    event.preventDefault();
    onSearch(typed.trim());
  }
  return (
    <form
      role="search"
      aria-label={`Search ${label}`}
      onSubmit={submit}
      className={`flex flex-wrap items-center gap-2 ${className}`}
    >
      <Input
        type="search"
        aria-label={`Search ${label}`}
        placeholder={placeholder}
        className="w-full max-w-xs"
        value={typed}
        onChange={(event) => setTyped(event.target.value)}
      />
      <Button type="submit" variant="ghost" size="sm">
        Search
      </Button>
      {query ? (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => {
            setTyped("");
            onSearch("");
          }}
        >
          Clear
        </Button>
      ) : null}
    </form>
  );
}
