"use client";

import { useCallback, useEffect, useState } from "react";

import { Alert, Button, Field, Input } from "@/components/ui/primitives";
import { buildSourceQueries, lookupOrganism, lookupProduct } from "@/lib/api";

/** Wait this long after typing stops before asking the upstream: NCBI rate-limits. */
const TYPING_PAUSE_MS = 400;

type Suggestion = { key: string; label: string; detail: string | null; pick: () => Promise<string[]> };

/**
 * One name lookup: type, choose a suggestion, keep the names that should count.
 * Every name starts chosen; untick a name the literature does not use.
 */
function NameLookup({
  label,
  placeholder,
  search,
  onTerms,
}: {
  label: string;
  placeholder: string;
  search: (text: string) => Promise<Suggestion[]>;
  onTerms: (terms: string[]) => void;
}) {
  const [text, setText] = useState("");
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [terms, setTerms] = useState<string[]>([]);
  const [kept, setKept] = useState<string[]>([]);
  const [problem, setProblem] = useState<string | null>(null);
  //: The label of the suggestion chosen last: no need to search for it again.
  const [chosen, setChosen] = useState<string | null>(null);

  useEffect(() => {
    if (text.trim().length < 3 || text === chosen) {
      setSuggestions([]);
      return;
    }
    let current = true;
    const timer = window.setTimeout(() => {
      search(text.trim())
        .then((items) => current && setSuggestions(items))
        .catch((caught) => current && setProblem(caught instanceof Error ? caught.message : "lookup failed"));
    }, TYPING_PAUSE_MS);
    return () => {
      current = false;
      window.clearTimeout(timer);
    };
  }, [text, chosen, search]);

  useEffect(() => onTerms(kept), [kept, onTerms]);

  async function choose(suggestion: Suggestion) {
    setProblem(null);
    try {
      const found = await suggestion.pick();
      setTerms(found);
      setKept(found);
      setSuggestions([]);
      setChosen(suggestion.label);
      setText(suggestion.label);
    } catch (caught) {
      setProblem(caught instanceof Error ? caught.message : "lookup failed");
    }
  }

  return (
    <div className="space-y-2">
      <Field label={label}>
        <Input value={text} placeholder={placeholder} onChange={(event) => setText(event.target.value)} />
      </Field>
      {suggestions.length > 0 ? (
        <ul aria-label={`${label} suggestions`} className="space-y-1 text-sm">
          {suggestions.map((suggestion) => (
            <li key={suggestion.key}>
              <button
                type="button"
                className="text-link underline hover:no-underline"
                onClick={() => choose(suggestion)}
              >
                {suggestion.label}
              </button>
              {suggestion.detail ? <span className="ml-1.5 text-xs text-muted">{suggestion.detail}</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
      {terms.length > 0 ? (
        <fieldset className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
          <legend className="sr-only">{label} names to search</legend>
          {terms.map((term) => (
            <label key={term} className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={kept.includes(term)}
                onChange={() =>
                  setKept((current) =>
                    current.includes(term) ? current.filter((item) => item !== term) : [...current, term],
                  )
                }
              />
              {term}
            </label>
          ))}
        </fieldset>
      ) : null}
      {problem ? (
        <Alert tone="warning" role="status">
          {problem} You can still write the queries by hand.
        </Alert>
      ) : null}
    </div>
  );
}

/**
 * Write a scope's source queries from an organism and a product.
 *
 * Organisms come from NCBI Taxonomy with their synonyms (so *Candida lipolytica*
 * papers are not missed when searching for *Yarrowia lipolytica*), products from
 * PubChem with registry codes removed. The queries this writes are only a
 * starting point: they land in the fields below, to be checked and edited.
 */
export function QueryHelper({ onQueries }: { onQueries: (queries: Record<string, string>) => void }) {
  const [organismTerms, setOrganismTerms] = useState<string[]>([]);
  const [productTerms, setProductTerms] = useState<string[]>([]);
  const [problem, setProblem] = useState<string | null>(null);

  const searchOrganisms = useCallback(async (text: string): Promise<Suggestion[]> =>
    (await lookupOrganism({ q: text })).suggestions.map((item) => ({
      key: String(item.taxid),
      label: item.scientific_name,
      detail: [item.rank, item.common_name, `taxid ${item.taxid}`].filter(Boolean).join(" · "),
      pick: async () => (await lookupOrganism({ taxid: item.taxid })).seed?.search_terms ?? [item.scientific_name],
    })), []);

  const searchProducts = useCallback(async (text: string): Promise<Suggestion[]> =>
    (await lookupProduct({ q: text })).suggestions.map((item) => ({
      key: item.name,
      label: item.name,
      detail: null,
      pick: async () => (await lookupProduct({ q: item.name, resolve: true })).seed?.search_terms ?? [item.name],
    })), []);

  return (
    <section aria-label="Write queries from an organism and a product" className="space-y-3 rounded-control border border-border bg-surface p-4">
      <p className="text-sm font-medium text-ink">Write the queries from an organism and a product</p>
      <div className="grid gap-4 sm:grid-cols-2">
        <NameLookup label="Organism" placeholder="Yarrowia lipolytica" search={searchOrganisms} onTerms={setOrganismTerms} />
        <NameLookup label="Product (optional)" placeholder="oleic acid" search={searchProducts} onTerms={setProductTerms} />
      </div>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        disabled={organismTerms.length + productTerms.length === 0}
        onClick={async () => {
          try {
            onQueries(await buildSourceQueries(organismTerms, productTerms));
            setProblem(null);
          } catch (caught) {
            setProblem(caught instanceof Error ? caught.message : "the queries were not written");
          }
        }}
      >
        Write the source queries ({organismTerms.length + productTerms.length} names)
      </Button>
      {problem ? (
        <Alert tone="danger" role="alert">
          {problem}
        </Alert>
      ) : null}
    </section>
  );
}
