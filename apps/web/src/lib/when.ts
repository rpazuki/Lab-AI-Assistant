/**
 * A time as the pages show it, and as their searches match it: `2026-11-23 16:17 UTC`.
 *
 * UTC, written the way the server writes a time for a search, so typing what you
 * see in a When column (a day, or a day and a time) finds the row. A local-time
 * display would differ from the server's text by the time zone, and a search for
 * the time on screen would quietly find nothing.
 */
export function utcWhen(iso: string | null | undefined): string {
  if (!iso) return "—";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  return `${at.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}
