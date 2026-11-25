"use client";

import { useCallback, useEffect, useState } from "react";

import { CollapsiblePanel } from "@/components/ui/CollapsiblePanel";
import { Alert, Button } from "@/components/ui/primitives";
import { buildAbstractDocuments, getAbstractFallback, setAbstractFallback } from "@/lib/api";
import { utcWhen } from "@/lib/when";
import type { AbstractFallback as Fallback } from "@/types";

const n = (value: number) => value.toLocaleString();

/**
 * Abstracts in place of full texts nobody could fetch.
 *
 * Off until a steward switches it on: indexing and quoting the abstracts of
 * papers whose licence does not allow keeping a copy is a rights decision, and
 * the switch records who made it. On, each included paper with an abstract and
 * no file becomes **partial**: its abstract is in the next release, and Ask
 * marks every source that is an abstract. An upload later replaces it.
 */
export function AbstractFallback({ onChanged }: { onChanged?: () => void }) {
  const [view, setView] = useState<Fallback | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setView(await getAbstractFallback());
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the setting could not be loaded");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function build() {
    let built = 0;
    let refusal: string | null = null;
    try {
      for (let round = 0; round < 200; round++) {
        setBusy(`Building abstract documents: ${n(built)} so far`);
        const step = await buildAbstractDocuments();
        built += step.built;
        if (step.remaining === 0 || step.built === 0) break;
      }
      setNotice(
        `${n(built)} paper${built === 1 ? "" : "s"} now stand in as their abstract and are parsed ` +
          "for the next release.",
      );
      onChanged?.();
    } catch (caught) {
      refusal = caught instanceof Error ? caught.message : "building stopped part way; press again";
    } finally {
      setBusy(null);
      // `load` clears the error when it succeeds, so the refusal is set after
      // it; setting it first would wipe it and leave the press looking idle.
      await load();
      if (refusal !== null) setError(refusal);
    }
  }

  async function toggle(enabled: boolean) {
    if (
      enabled &&
      !window.confirm(
        "Use abstracts in place of full texts that could not be fetched?\n\n" +
          "Their abstracts will be indexed and quoted in Ask answers, marked as abstracts. " +
          "This is recorded as your decision. Switching it off takes them out of the next release.",
      )
    ) {
      return;
    }
    setError(null);
    setNotice(null);
    try {
      setView(await setAbstractFallback(enabled));
      if (enabled) await build();
      else setNotice("Switched off: abstracts are left out of the next release.");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the setting could not be saved");
    }
  }

  return (
    <CollapsiblePanel title="Abstracts for papers without full text" storageKey="collections.abstracts">
      <div className="space-y-3 px-5 py-3 text-sm">
        {error ? (
          <Alert tone="danger" role="alert">
            {error}
          </Alert>
        ) : null}
        {notice ? (
          <Alert tone="success" role="status">
            {notice}
          </Alert>
        ) : null}
        <p className="max-w-3xl text-xs text-muted">
          A paper no route could fetch is missing from Ask until someone uploads it. With this on,
          its abstract stands in for it: the plan is marked <strong>partial</strong>, the abstract
          goes into the next release, and every Ask source taken from an abstract is marked so, in
          the sources and in the answer. Uploading the full text later replaces it. No values are
          extracted from abstracts.
        </p>
        {view ? (
          <>
            <label className="flex items-center gap-2 text-sm text-ink">
              <input
                type="checkbox"
                checked={view.enabled}
                disabled={busy !== null}
                onChange={(event) => void toggle(event.target.checked)}
              />
              <span>Use abstracts in place of full texts that could not be fetched</span>
            </label>
            <p className="text-xs text-muted" aria-live="polite">
              {view.enabled && view.set_at ? `On since ${utcWhen(view.set_at)}. ` : ""}
              {n(view.partial)} paper{view.partial === 1 ? "" : "s"} stand in as their abstract;{" "}
              {n(view.eligible)} more could (included, an abstract on record, no file).
            </p>
            {view.enabled && view.eligible > 0 ? (
              <Button size="sm" disabled={busy !== null} onClick={() => void build()}>
                Use abstracts for {n(view.eligible)} more
              </Button>
            ) : null}
            {busy ? <p className="text-xs text-muted">{busy}</p> : null}
          </>
        ) : (
          <p className="text-xs text-muted">Loading…</p>
        )}
      </div>
    </CollapsiblePanel>
  );
}
