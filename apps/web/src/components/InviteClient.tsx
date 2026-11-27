"use client";

import { useEffect, useState } from "react";

import { resetSidebarGroups } from "@/components/Sidebar";
import { Alert, Button, Field, Input } from "@/components/ui/primitives";
import { acceptInvitation, previewInvitation } from "@/lib/api";
import { appPath } from "@/lib/config";
import { HOME_PATH } from "@/lib/navigation";
import type { InvitationPreview } from "@/types";

/** The minimum the API accepts; said here so the form can say it first. */
const MIN_PASSWORD = 12;

/** The token from `#token=…`. A fragment never leaves the browser in a request. */
export function tokenFromHash(hash: string): string | null {
  const value = new URLSearchParams(hash.replace(/^#/, "")).get("token");
  return value && value.length >= 16 ? value : null;
}

type Stage =
  | { kind: "checking" }
  | { kind: "invalid" }
  | { kind: "ready"; token: string; preview: InvitationPreview };

/**
 * Accept an invitation: see what it offers, choose a password, and arrive signed in.
 *
 * Someone who already has an account confirms with their existing password
 * instead, since the link proves the invitation reached them, not that its holder
 * may set a new password on an account that exists. The token is taken out of
 * the address bar as soon as it is read, so it is not left in the history for the
 * next person at this browser.
 */
export function InviteClient() {
  const [stage, setStage] = useState<Stage>({ kind: "checking" });
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const token = tokenFromHash(window.location.hash);
    if (window.location.hash) {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    if (!token) {
      setStage({ kind: "invalid" });
      return;
    }
    previewInvitation(token)
      .then((preview) => setStage({ kind: "ready", token, preview }))
      .catch(() => setStage({ kind: "invalid" }));
  }, []);

  if (stage.kind === "checking") {
    return <p className="text-center text-sm text-muted">Checking the invitation…</p>;
  }
  if (stage.kind === "invalid") {
    return (
      <Alert tone="danger" role="alert">
        This invitation is not valid. It may have expired, been replaced by a newer one, or
        already been used. Ask an administrator for a new link.
      </Alert>
    );
  }

  const { preview, token } = stage;
  const newAccount = !preview.has_account;

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    if (newAccount && password.length < MIN_PASSWORD) {
      setError(`Choose a password of at least ${MIN_PASSWORD} characters.`);
      return;
    }
    if (newAccount && password !== confirm) {
      setError("The two passwords differ.");
      return;
    }
    setBusy(true);
    try {
      await acceptInvitation({
        token,
        password,
        full_name: newAccount && fullName.trim() ? fullName.trim() : undefined,
      });
      resetSidebarGroups();
      // A full load, so every part of the page starts from the new session.
      window.location.assign(appPath(HOME_PATH));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the invitation could not be accepted");
      setBusy(false);
    }
  }

  return (
    <form method="post" onSubmit={onSubmit} className="space-y-4">
      <p className="text-sm text-ink">
        You are invited to <strong>{preview.workspace.name}</strong> as a{" "}
        <strong>{preview.role.replace(/_/g, " ")}</strong>.
      </p>
      <Field label="Email">
        <Input type="email" value={preview.email} readOnly autoComplete="username" />
      </Field>
      {newAccount ? (
        <>
          <Field label="Full name (optional)">
            <Input
              autoComplete="name"
              value={fullName}
              onChange={(event) => setFullName(event.target.value)}
            />
          </Field>
          <Field label={`Password (at least ${MIN_PASSWORD} characters)`}>
            <Input
              type="password"
              autoComplete="new-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </Field>
          <Field label="Repeat the password">
            <Input
              type="password"
              autoComplete="new-password"
              required
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
            />
          </Field>
        </>
      ) : (
        <Field label="Your existing password">
          <Input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </Field>
      )}
      {error ? (
        <Alert tone="danger" role="alert">
          {error}
        </Alert>
      ) : null}
      <Button type="submit" disabled={busy} className="w-full">
        {busy ? "Joining…" : newAccount ? "Create account and join" : "Join"}
      </Button>
    </form>
  );
}
