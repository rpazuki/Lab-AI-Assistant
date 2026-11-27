"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { ApiError, login, setFirstPassword } from "@/lib/api";
import { HOME_PATH } from "@/lib/navigation";
import { resetSidebarGroups } from "@/components/Sidebar";
import { Alert, Button, Field, Input } from "@/components/ui/primitives";

/** The minimum the API accepts; said here so the form can say it first. */
const MIN_PASSWORD = 12;

/**
 * Sign in, and on a first sign-in with a temporary password, choose one's own.
 *
 * A temporary password is what an admin sends instead of an invitation link. It
 * works once: the API answers it with "choose a password of your own", and this
 * form turns into that step, keeping the address and the temporary password it
 * was given so the person types neither again.
 *
 * Both forms say `method="post"`. Sent before the page's script has loaded, a
 * form without one is a GET, which puts the password in the address bar, the
 * browser's history and the web server's access log.
 *
 * `next` is where to go once signed in; the page has already checked it stays in this app.
 */
export function LoginForm({ next = HOME_PATH }: { next?: string }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [choosing, setChoosing] = useState(false);
  const [fullName, setFullName] = useState("");
  const [chosen, setChosen] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function arrive() {
    resetSidebarGroups();
    router.push(next);
  }

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      arrive();
    } catch (caught) {
      if (caught instanceof ApiError && caught.context.reason === "password_change_required") {
        setChoosing(true);
      } else {
        // The message never says which half was wrong.
        setError(caught instanceof Error ? caught.message : "sign-in failed");
      }
    } finally {
      setBusy(false);
    }
  }

  async function onChoose(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    if (chosen.length < MIN_PASSWORD) {
      setError(`Choose a password of at least ${MIN_PASSWORD} characters.`);
      return;
    }
    if (chosen !== confirm) {
      setError("The two passwords differ.");
      return;
    }
    setBusy(true);
    try {
      await setFirstPassword({
        email,
        temporary_password: password,
        password: chosen,
        full_name: fullName.trim() || undefined,
      });
      arrive();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the password could not be set");
      setBusy(false);
    }
  }

  const failure = error ? (
    <Alert tone="danger" role="alert">
      {error}
    </Alert>
  ) : null;

  if (choosing) {
    return (
      <form method="post" onSubmit={onChoose} className="space-y-4">
        <Alert tone="info" role="status">
          Your temporary password is correct. It works only once, so choose a password of your
          own to finish signing in.
        </Alert>
        <Field label="Email">
          <Input type="email" value={email} readOnly autoComplete="username" />
        </Field>
        <Field label="Full name (optional)">
          <Input
            autoComplete="name"
            value={fullName}
            onChange={(event) => setFullName(event.target.value)}
          />
        </Field>
        <Field label={`New password (at least ${MIN_PASSWORD} characters)`}>
          <Input
            type="password"
            autoComplete="new-password"
            required
            value={chosen}
            onChange={(event) => setChosen(event.target.value)}
          />
        </Field>
        <Field label="Repeat the new password">
          <Input
            type="password"
            autoComplete="new-password"
            required
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
          />
        </Field>
        {failure}
        <Button type="submit" disabled={busy} className="w-full">
          {busy ? "Saving…" : "Set password and sign in"}
        </Button>
      </form>
    );
  }

  return (
    <form method="post" onSubmit={onSubmit} className="space-y-4">
      <Field label="Email">
        <Input
          type="email"
          name="email"
          autoComplete="username"
          required
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
      </Field>
      <Field label="Password">
        <Input
          type="password"
          name="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
      </Field>
      {failure}
      <Button type="submit" disabled={busy} className="w-full">
        {busy ? "Signing in…" : "Sign in"}
      </Button>
      <p className="text-center text-xs text-muted">
        Were you sent a temporary password? Sign in with it here, then choose your own.
      </p>
    </form>
  );
}
