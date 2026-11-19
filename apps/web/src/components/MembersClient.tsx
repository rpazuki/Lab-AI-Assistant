"use client";

import { useCallback, useEffect, useState } from "react";

import {
  Alert,
  Button,
  Empty,
  Field,
  Input,
  Panel,
  StateChip,
} from "@/components/ui/primitives";
import {
  createInvitation,
  getMe,
  invitationLink,
  listInvitations,
  listMembers,
  revokeInvitation,
  revokeMemberSessions,
  updateMember,
} from "@/lib/api";
import type { Invitation, Member } from "@/types";

/** The roles a person can hold, in order of reach. A service account is not one. */
export const ROLES = ["researcher", "reviewer", "data_steward", "admin"] as const;

const roleLabel = (role: string) => role.replace(/_/g, " ");

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

const selectClass =
  "rounded-control border border-border-strong bg-surface px-2 py-1 text-sm text-ink shadow-control " +
  "disabled:cursor-not-allowed disabled:opacity-45";

/**
 * Members of the workspace and the invitations that bring new ones in.
 *
 * There is no mailer: creating an invitation shows its link once, for the admin
 * to send however they like. The token is never stored, so a lost link is
 * replaced by inviting the address again, which also revokes the old one.
 *
 * The API enforces the rules this page reflects — nobody changes their own
 * membership, only an admin grants admin — so a control that is disabled here is
 * a courtesy, not the control.
 */
export function MembersClient() {
  const [members, setMembers] = useState<Member[]>([]);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [selfId, setSelfId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [email, setEmail] = useState("");
  const [role, setRole] = useState<string>("researcher");
  const [days, setDays] = useState("7");
  const [link, setLink] = useState<{ email: string; url: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [nextMembers, nextInvitations, me] = await Promise.all([
        listMembers(),
        listInvitations(),
        getMe(),
      ]);
      setMembers(nextMembers);
      setInvitations(nextInvitations);
      setSelfId(me.user_id);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "members could not be loaded");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  /**
   * Run a change, reload, then report. The report comes after the reload because
   * a successful reload clears the error banner, and a refusal must stay visible.
   */
  async function act(action: () => Promise<unknown>, done: string) {
    setNotice(null);
    let failure: string | null = null;
    try {
      await action();
    } catch (caught) {
      failure = caught instanceof Error ? caught.message : "the change was not made";
    }
    await refresh();
    if (failure) setError(failure);
    else setNotice(done);
  }

  async function invite(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setNotice(null);
    setCopied(false);
    let failure: string | null = null;
    try {
      const hours = Math.round(Number(days) * 24);
      const created = await createInvitation({ email: email.trim(), role, ttl_hours: hours });
      setLink({ email: created.email, url: invitationLink(created.token) });
      setEmail("");
    } catch (caught) {
      failure = caught instanceof Error ? caught.message : "the invitation was not created";
    } finally {
      setBusy(false);
    }
    await refresh();
    if (failure) setError(failure);
  }

  async function copy() {
    if (!link) return;
    try {
      await navigator.clipboard.writeText(link.url);
      setCopied(true);
    } catch {
      // Clipboard access refused: the link is on screen to select by hand.
    }
  }

  const open = invitations.filter((item) => item.state === "open");
  const past = invitations.filter((item) => item.state !== "open");

  return (
    <div className="space-y-4">
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

      <Panel title="Invite someone">
        <form onSubmit={invite} className="grid gap-3 sm:grid-cols-[1fr_12rem_8rem_auto] sm:items-end">
          <Field label="Email">
            <Input
              type="email"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="name@imperial.ac.uk"
            />
          </Field>
          <Field label="Role">
            <select
              className={`${selectClass} w-full py-2`}
              value={role}
              onChange={(event) => setRole(event.target.value)}
            >
              {ROLES.map((item) => (
                <option key={item} value={item}>
                  {roleLabel(item)}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Valid for (days)">
            <Input
              type="number"
              min={1}
              max={30}
              required
              value={days}
              onChange={(event) => setDays(event.target.value)}
            />
          </Field>
          <Button type="submit" disabled={busy || !email.trim()}>
            Create link
          </Button>
        </form>
        {link ? (
          <div className="mt-4 space-y-2">
            <Alert tone="info" role="status">
              Send this link to {link.email}. It is shown only now: if it is lost, invite the
              address again.
            </Alert>
            <div className="flex flex-wrap items-center gap-2">
              <Input readOnly value={link.url} aria-label="Invitation link" className="flex-1 font-mono text-xs" />
              <Button type="button" variant="ghost" onClick={copy}>
                {copied ? "Copied" : "Copy link"}
              </Button>
            </div>
          </div>
        ) : null}
      </Panel>

      <Panel title={`Members (${members.length})`} flush>
        {members.length === 0 ? (
          <div className="px-5 py-3">
            <Empty>No members yet.</Empty>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Member</th>
                  <th>Role</th>
                  <th>Status</th>
                  <th>Last sign-in</th>
                  <th>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {members.map((member) => {
                  const self = member.user_id === selfId;
                  const suspended = member.status === "suspended";
                  return (
                    <tr key={member.user_id}>
                      <td>
                        <span className="block font-medium text-ink">
                          {member.full_name ?? member.email}
                          {self ? <span className="ml-1.5 text-xs text-muted">(you)</span> : null}
                        </span>
                        {member.full_name ? (
                          <span className="block text-xs text-muted">{member.email}</span>
                        ) : null}
                      </td>
                      <td>
                        <select
                          aria-label={`Role of ${member.email}`}
                          className={selectClass}
                          value={member.role}
                          disabled={self}
                          onChange={(event) =>
                            act(
                              () => updateMember(member.user_id, { role: event.target.value }),
                              `${member.email} is now ${roleLabel(event.target.value)}.`,
                            )
                          }
                        >
                          {ROLES.map((item) => (
                            <option key={item} value={item}>
                              {roleLabel(item)}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td>
                        <StateChip state={member.status} />
                      </td>
                      <td className="text-xs text-muted">{when(member.last_login_at)}</td>
                      <td>
                        <div className="flex justify-end gap-2">
                          <Button
                            variant="ghost"
                            size="sm"
                            disabled={self || suspended}
                            onClick={() =>
                              act(
                                () => revokeMemberSessions(member.user_id),
                                `${member.email} has been signed out everywhere.`,
                              )
                            }
                          >
                            Sign out everywhere
                          </Button>
                          {suspended ? (
                            <Button
                              variant="ghost"
                              size="sm"
                              disabled={self}
                              onClick={() =>
                                act(
                                  () => updateMember(member.user_id, { status: "active" }),
                                  `${member.email} is reinstated.`,
                                )
                              }
                            >
                              Reinstate
                            </Button>
                          ) : (
                            <Button
                              variant="danger"
                              size="sm"
                              disabled={self}
                              onClick={() => {
                                if (
                                  window.confirm(
                                    `Suspend ${member.email}? They are signed out at once and cannot sign in until reinstated.`,
                                  )
                                ) {
                                  void act(
                                    () => updateMember(member.user_id, { status: "suspended" }),
                                    `${member.email} is suspended.`,
                                  );
                                }
                              }}
                            >
                              Suspend
                            </Button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel title="Invitations" flush>
        {invitations.length === 0 ? (
          <div className="px-5 py-3">
            <Empty>No invitations yet.</Empty>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Email</th>
                  <th>Role</th>
                  <th>State</th>
                  <th>Expires</th>
                  <th>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {[...open, ...past].map((item) => (
                  <tr key={item.id}>
                    <td>{item.email}</td>
                    <td>{roleLabel(item.role)}</td>
                    <td>
                      <StateChip state={item.state} />
                    </td>
                    <td className="text-xs text-muted">
                      {item.state === "accepted" ? `accepted ${when(item.accepted_at)}` : when(item.expires_at)}
                    </td>
                    <td>
                      {item.state === "open" ? (
                        <div className="flex justify-end">
                          <Button
                            variant="danger"
                            size="sm"
                            onClick={() =>
                              act(() => revokeInvitation(item.id), `The invitation to ${item.email} is revoked.`)
                            }
                          >
                            Revoke
                          </Button>
                        </div>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
