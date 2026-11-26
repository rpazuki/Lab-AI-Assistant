"use client";

import { useCallback, useEffect, useState } from "react";

import { PanelFrame } from "@/components/ui/CollapsiblePanel";
import { Alert, Button, Field, Input } from "@/components/ui/primitives";
import { getParseQuality, setParseLimits } from "@/lib/api";
import type { ParseQuality as ParseQualityView } from "@/types";

const FORMATS: Record<string, string> = { "application/pdf": "PDF", "application/xml": "XML (JATS)" };

const percent = (ratio: number) => `${Math.round(ratio * 1000) / 10}%`;

/**
 * The two parse-quality limits that decide most degraded documents, for a
 * steward to adjust. A degraded document is neither extracted nor released.
 * New parses use the limits at once; documents already degraded are checked
 * against them again on the next press of **Fetch included papers**, from the
 * metrics their parse recorded, without parsing anything again.
 */
export function ParseQuality({
  refreshKey,
  embedded = false,
}: {
  refreshKey?: unknown;
  /** Shown inside Collection settings rather than as its own panel. */
  embedded?: boolean;
}) {
  const [view, setView] = useState<ParseQualityView | null>(null);
  const [ratio, setRatio] = useState("");
  const [separation, setSeparation] = useState("default");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const show = useCallback((next: ParseQualityView) => {
    setView(next);
    const limit = next.limits.max_duplicate_paragraph_ratio;
    setRatio(limit === null ? "" : String(Math.round(limit * 1000) / 10));
    const required = next.limits.require_reference_separation;
    setSeparation(required === null ? "default" : required ? "yes" : "no");
  }, []);

  useEffect(() => {
    getParseQuality()
      .then((next) => {
        show(next);
        setError(null);
      })
      .catch((caught) => setError(caught instanceof Error ? caught.message : "the limits could not be loaded"));
  }, [show, refreshKey]);

  async function save() {
    setSaving(true);
    setNotice(null);
    try {
      const next = await setParseLimits({
        max_duplicate_paragraph_ratio: ratio.trim() === "" ? null : Number(ratio) / 100,
        require_reference_separation: separation === "default" ? null : separation === "yes",
      });
      show(next);
      setError(null);
      setNotice(
        `Saved. New parses use these limits now. The ${next.degraded.toLocaleString()} degraded ` +
          `documents are checked against them when you next press Fetch included papers.`,
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the limits were not saved");
    } finally {
      setSaving(false);
    }
  }

  const defaults = view?.defaults ?? {};
  return (
    <PanelFrame
      embedded={embedded}
      title="Parse quality"
      storageKey="collections.parse-quality"
      count={
        view ? (
          <span className="text-xs text-muted">{view.degraded.toLocaleString()} degraded documents</span>
        ) : null
      }
    >
      <div className="space-y-4 text-sm">
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
        <p className="text-xs text-muted">
          A parsed document that misses a limit is degraded: it is not extracted or released. These two
          limits decide most degraded documents. New parses use them at once; documents already degraded
          are checked again when you next press <strong>Fetch included papers</strong>, from what their
          parse measured, without parsing them again. Loosening a limit can only add documents; a
          document already accepted is never taken out.
        </p>
        {view && Object.keys(view.degraded_by_reason).length > 0 ? (
          <ul aria-label="Why documents are degraded" className="list-disc pl-5 text-xs text-muted">
            {Object.entries(view.degraded_by_reason).map(([reason, count]) => (
              <li key={reason}>
                {count.toLocaleString()} — {reason}
              </li>
            ))}
          </ul>
        ) : null}
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Repeated paragraphs allowed, up to (%)">
            <Input
              type="number"
              min={0}
              max={100}
              step={0.5}
              placeholder={Object.entries(defaults)
                .map(([type, d]) => `${FORMATS[type] ?? type} ${percent(d.max_duplicate_paragraph_ratio)}`)
                .join(", ")}
              value={ratio}
              onChange={(event) => setRatio(event.target.value)}
            />
          </Field>
          <Field label="Reference list must be separated from the text">
            <select
              className="w-full rounded-control border border-border-strong bg-surface px-3 py-2 text-sm text-ink"
              value={separation}
              onChange={(event) => setSeparation(event.target.value)}
            >
              <option value="default">
                Each format&apos;s default (
                {Object.entries(defaults)
                  .map(([type, d]) => `${FORMATS[type] ?? type}: ${d.require_reference_separation ? "yes" : "no"}`)
                  .join(", ")}
                )
              </option>
              <option value="yes">Yes, for every document</option>
              <option value="no">No, for every document</option>
            </select>
          </Field>
        </div>
        <p className="text-xs text-muted">
          Leave the share empty to keep each format&apos;s default.
          {view?.set_at ? ` Last changed ${new Date(view.set_at).toISOString().slice(0, 16).replace("T", " ")} UTC.` : ""}
        </p>
        <Button size="sm" disabled={saving || !view} onClick={save}>
          Save limits
        </Button>
      </div>
    </PanelFrame>
  );
}
