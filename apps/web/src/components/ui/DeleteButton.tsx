"use client";

import { Button } from "@/components/ui/primitives";

/**
 * The one Delete button. It is shown only where the server would allow the
 * deletion, so its absence is the answer to "can I delete this?"; it always asks
 * first, saying what goes and that the deletion is permanent and audited, and a
 * refusal the page could not foresee comes back as the page's error.
 */
export function DeleteButton({
  what,
  consequence,
  onDelete,
}: {
  /** What is deleted, as the question names it: "discovery run 3f2a1b9c". */
  what: string;
  /** What else goes or changes with it, in one sentence. */
  consequence: string;
  onDelete: () => void;
}) {
  return (
    <Button
      variant="danger"
      size="sm"
      aria-label={`Delete ${what}`}
      onClick={() => {
        if (
          window.confirm(
            `Delete ${what}? ${consequence} The deletion is recorded in the audit log and cannot be undone.`,
          )
        ) {
          onDelete();
        }
      }}
    >
      Delete
    </Button>
  );
}
