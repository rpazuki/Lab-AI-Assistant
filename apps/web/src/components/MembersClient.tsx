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
import { DeleteButton } from "@/components/ui/DeleteButton";
import { SortHeader, TableSearch } from "@/components/ui/table";
import {
  createInvitation,
  deleteInvitation,
  getMe,
  getMemberSession,
  invitationLink,
  listInvitations,
  listMemberSessions,
  listMembers,
  revokeInvitation,
  revokeMemberSessions,
  updateMember,
} from "@/lib/api";
import { useTable } from "@/lib/useTable";
import type { ChatMessage, Invitation, Member, MemberSession } from "@/types";

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
  const [defaultLimit, setDefaultLimit] = useState<number | null>(null);
  const [oversight, setOversight] = useState(false);
  const [reading, setReading] = useState<Member | null>(null);
  const [editingLimit, setEditingLimit] = useState<string | null>(null);
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
      setMembers(nextMembers.items);
      setDefaultLimit(nextMembers.token_limit_default);
      setOversight(nextMembers.chat_admin_access);
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

  const people = useTable(members, {
    columns: {
      member: (member) => member.full_name ?? member.email,
      role: (member) => member.role,
      status: (member) => member.status,
      usage: (member) => member.usage.total_tokens,
      last: (member) => member.last_login_at,
    },
    defaultSort: { key: "member", direction: "asc" },
    searchText: (member) =>
      `${member.full_name ?? ""} ${member.email} ${member.role} ${member.status}`,
  });
  const invites = useTable(invitations, {
    columns: {
      email: (item) => item.email,
      role: (item) => item.role,
      state: (item) => item.state,
      expires: (item) => item.expires_at,
    },
    // Newest first: the invitation just sent is the one being checked.
    defaultSort: { key: "expires", direction: "desc" },
    searchText: (item) => `${item.email} ${item.role} ${item.state}`,
  });

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

      <UsageTiles members={members} />

      <Panel title={`Members (${members.length})`} flush>
        {members.length === 0 ? (
          <div className="px-5 py-3">
            <Empty>No members yet.</Empty>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <TableSearch table={people} label="members" placeholder="Filter by name, email, role or status" />
            <table className="data-table">
              <thead>
                <tr>
                  <SortHeader table={people} column="member">
                    Member
                  </SortHeader>
                  <SortHeader table={people} column="role">
                    Role
                  </SortHeader>
                  <SortHeader table={people} column="status">
                    Status
                  </SortHeader>
                  <SortHeader table={people} column="usage">
                    Usage
                  </SortHeader>
                  <SortHeader table={people} column="last">
                    Last sign-in
                  </SortHeader>
                  <th>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {people.rows.map((member) => {
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
                        <StateChip state={member.status} kind="membership" />
                      </td>
                      <td className="min-w-56">
                        {editingLimit === member.user_id ? (
                          <LimitEditor
                            member={member}
                            defaultLimit={defaultLimit}
                            onCancel={() => setEditingLimit(null)}
                            onSave={async (limit) => {
                              setEditingLimit(null);
                              await act(
                                () => updateMember(member.user_id, { token_limit: limit }),
                                limit === null
                                  ? `${member.email} is back on the default limit.`
                                  : `${member.email} may now use ${formatTokens(limit)} tokens.`,
                              );
                            }}
                          />
                        ) : (
                          <UsageCell member={member} onEdit={() => setEditingLimit(member.user_id)} />
                        )}
                      </td>
                      <td className="text-xs text-muted">{when(member.last_login_at)}</td>
                      <td>
                        <div className="flex justify-end gap-2">
                          {oversight ? (
                            <Button
                              variant="ghost"
                              size="sm"
                              aria-pressed={reading?.user_id === member.user_id}
                              onClick={() =>
                                setReading(reading?.user_id === member.user_id ? null : member)
                              }
                            >
                              Read chats
                            </Button>
                          ) : null}
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

      {reading ? <MemberChats member={reading} onClose={() => setReading(null)} /> : null}

      <Panel title="Invitations" flush>
        {invitations.length === 0 ? (
          <div className="px-5 py-3">
            <Empty>No invitations yet.</Empty>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <TableSearch table={invites} label="invitations" placeholder="Filter by email, role or state" />
            <table className="data-table">
              <thead>
                <tr>
                  <SortHeader table={invites} column="email">
                    Email
                  </SortHeader>
                  <SortHeader table={invites} column="role">
                    Role
                  </SortHeader>
                  <SortHeader table={invites} column="state">
                    State
                  </SortHeader>
                  <SortHeader table={invites} column="expires">
                    Expires
                  </SortHeader>
                  <th>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {invites.rows.map((item) => (
                  <tr key={item.id}>
                    <td>{item.email}</td>
                    <td>{roleLabel(item.role)}</td>
                    <td>
                      <StateChip state={item.state} kind="invitation" />
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
                      {item.state === "revoked" || item.state === "expired" ? (
                        <div className="flex justify-end">
                          <DeleteButton
                            what={`the ${item.state} invitation to ${item.email}`}
                            consequence="Its link already does not work; it only leaves this list."
                            onDelete={() =>
                              void act(
                                () => deleteInvitation(item.id),
                                `The invitation to ${item.email} is deleted.`,
                              )
                            }
                          />
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

export function formatTokens(count: number): string {
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(count % 1_000_000 ? 2 : 0)}M`;
  if (count >= 1_000) return `${(count / 1_000).toFixed(count % 1_000 ? 1 : 0)}k`;
  return String(count);
}

/** The workspace at a glance: the admin dashboard the predecessor had. */
function UsageTiles({ members }: { members: Member[] }) {
  const active = members.filter((member) => member.status === "active").length;
  const questions = members.reduce((sum, member) => sum + member.usage.questions, 0);
  const tokens = members.reduce((sum, member) => sum + member.usage.total_tokens, 0);
  const atLimit = members.filter((member) => member.limit_reached).length;
  const tiles: [string, string][] = [
    ["Active members", `${active} of ${members.length}`],
    ["Questions asked", String(questions)],
    ["Tokens used", formatTokens(tokens)],
    ["At their limit", String(atLimit)],
  ];
  return (
    <dl aria-label="Workspace usage" className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {tiles.map(([label, value]) => (
        <div key={label} className="rounded-card border border-border bg-surface px-5 py-4 shadow-card">
          <dt className="text-pretitle font-semibold uppercase tracking-wider text-muted">{label}</dt>
          <dd className="mt-1 text-stat font-semibold text-heading tabular-nums">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function UsageCell({ member, onEdit }: { member: Member; onEdit: () => void }) {
  const share =
    member.token_limit > 0 ? Math.min(1, member.usage.total_tokens / member.token_limit) : 1;
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="tabular-nums text-ink">
          {formatTokens(member.usage.total_tokens)} / {formatTokens(member.token_limit)} tokens
          {member.custom_token_limit ? "" : " (default)"}
        </span>
        <button type="button" className="text-link underline hover:no-underline" onClick={onEdit}>
          Change limit
        </button>
      </div>
      <div
        role="meter"
        aria-label={`Tokens used by ${member.email}`}
        aria-valuemin={0}
        aria-valuemax={member.token_limit}
        aria-valuenow={member.usage.total_tokens}
        className="h-1.5 overflow-hidden rounded-badge bg-neutral-lt"
      >
        <div
          className={`h-full ${member.limit_reached ? "bg-danger" : share > 0.8 ? "bg-warning" : "bg-accent"}`}
          style={{ width: `${Math.round(share * 100)}%` }}
        />
      </div>
      <p className="text-xs text-muted">
        {member.usage.questions} question{member.usage.questions === 1 ? "" : "s"} in{" "}
        {member.usage.sessions} conversation{member.usage.sessions === 1 ? "" : "s"}
        {member.limit_reached ? " · limit reached" : ""}
      </p>
    </div>
  );
}

function LimitEditor({
  member,
  defaultLimit,
  onSave,
  onCancel,
}: {
  member: Member;
  defaultLimit: number | null;
  onSave: (limit: number | null) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(String(member.token_limit));
  const parsed = Number(value);
  const valid = value.trim() !== "" && Number.isInteger(parsed) && parsed >= 0;
  return (
    <form
      className="flex flex-wrap items-center gap-1.5"
      onSubmit={(event) => {
        event.preventDefault();
        if (valid) onSave(parsed);
      }}
    >
      <Input
        type="number"
        min={0}
        step={1000}
        aria-label={`Token limit for ${member.email}`}
        className="w-32 py-1"
        value={value}
        onChange={(event) => setValue(event.target.value)}
      />
      <Button type="submit" size="sm" disabled={!valid}>
        Save
      </Button>
      {member.custom_token_limit ? (
        <Button type="button" variant="ghost" size="sm" onClick={() => onSave(null)}>
          Use default{defaultLimit !== null ? ` (${formatTokens(defaultLimit)})` : ""}
        </Button>
      ) : null}
      <Button type="button" variant="ghost" size="sm" onClick={onCancel}>
        Cancel
      </Button>
    </form>
  );
}

/**
 * A member's conversations, read by an admin.
 *
 * The workspace allows it and members are told so on the Ask page. Listing and
 * opening are both recorded in the audit log, which the banner says before
 * anything is shown.
 */
export function MemberChats({ member, onClose }: { member: Member; onClose: () => void }) {
  const [sessions, setSessions] = useState<MemberSession[] | null>(null);
  const [open, setOpen] = useState<(MemberSession & { messages: ChatMessage[] }) | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    setOpen(null);
    setSessions(null);
    listMemberSessions(member.user_id)
      .then((items) => current && setSessions(items))
      .catch(
        (caught) =>
          current && setError(caught instanceof Error ? caught.message : "chats could not be loaded"),
      );
    return () => {
      current = false;
    };
  }, [member.user_id]);

  async function read(session: MemberSession) {
    try {
      setOpen(await getMemberSession(member.user_id, session.id));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "the conversation could not be opened");
    }
  }

  return (
    <Panel
      title={`Conversations of ${member.full_name ?? member.email}`}
      aside={
        <Button variant="ghost" size="sm" onClick={onClose}>
          Close
        </Button>
      }
    >
      <Alert tone="info" role="status" className="mb-4">
        Reading a member&apos;s conversations is recorded in the audit log under your name.
      </Alert>
      {error ? (
        <Alert tone="danger" role="alert" className="mb-4">
          {error}
        </Alert>
      ) : null}
      {sessions === null ? (
        <Empty>Loading…</Empty>
      ) : sessions.length === 0 ? (
        <Empty>No conversations.</Empty>
      ) : (
        <div className="grid gap-4 md:grid-cols-[16rem_1fr]">
          <ul className="space-y-1 text-sm">
            {sessions.map((session) => (
              <li key={session.id}>
                <button
                  type="button"
                  aria-current={open?.id === session.id ? "true" : undefined}
                  onClick={() => read(session)}
                  className={`w-full rounded-control px-3 py-2 text-left transition-colors ${
                    open?.id === session.id
                      ? "bg-surface-subtle font-semibold"
                      : "hover:bg-surface-subtle"
                  }`}
                >
                  <span className="block truncate">{session.title ?? "Untitled"}</span>
                  <span className="block text-xs text-muted">
                    {when(session.updated_at)}
                    {session.deleted ? " · deleted by the member" : ""}
                  </span>
                </button>
              </li>
            ))}
          </ul>
          <div className="space-y-3">
            {open ? (
              open.messages.map((message) => (
                <div
                  key={message.id}
                  className={`rounded-control border border-border px-3 py-2 text-sm ${
                    message.role === "user" ? "bg-surface-subtle" : "bg-surface"
                  }`}
                >
                  <p className="mb-1 text-pretitle font-semibold uppercase tracking-wider text-muted">
                    {message.role === "user" ? "Question" : "Answer"} · {when(message.created_at)}
                  </p>
                  <p className="whitespace-pre-wrap text-ink">{message.content}</p>
                </div>
              ))
            ) : (
              <Empty>Choose a conversation to read it.</Empty>
            )}
          </div>
        </div>
      )}
    </Panel>
  );
}
