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
import { CollapsiblePanel } from "@/components/ui/CollapsiblePanel";
import { DeleteButton } from "@/components/ui/DeleteButton";
import { Pager } from "@/components/ui/Pager";
import { SortHeader, TableSearch } from "@/components/ui/table";
import {
  createInvitation,
  createRole,
  deleteInvitation,
  deleteRole,
  getMe,
  getMemberSession,
  invitationLink,
  listInvitations,
  listMemberSessions,
  listMembers,
  listRoles,
  revokeInvitation,
  revokeMemberSessions,
  updateMember,
  updateRole,
} from "@/lib/api";
import { usePager } from "@/lib/usePager";
import { useTable } from "@/lib/useTable";
import type { ChatMessage, Invitation, Member, MemberSession, Page, Role } from "@/types";

/**
 * The roles that ship with the product, in order of reach. A service account is
 * not one. They are listed here so the selects are never empty on first paint;
 * the workspace's own list replaces them as soon as it arrives, and it includes
 * any role an admin defined.
 */
export const ROLES = ["researcher", "reviewer", "data_steward", "admin"] as const;

/** The built-in roles a defined one can be based on. Never admin: see below. */
const BASE_ROLES = ["researcher", "reviewer", "data_steward"] as const;

const roleLabel = (role: string) => role.replace(/_/g, " ");

/** What to call a role on screen: the wording its admin gave it, else its name. */
const labelFor = (roles: Role[], name: string) =>
  roles.find((item) => item.name === name)?.display_name ?? roleLabel(name);

/**
 * The options for a role select: built-in ones first, then any defined here.
 *
 * The two are separated because the difference matters when the choice is about
 * who reaches confidential material — a role defined here is one somebody in
 * this workspace decided on, not one the product shipped.
 */
function RoleOptions({ roles }: { roles: Role[] }) {
  const choices: { name: string; label: string; custom: boolean }[] =
    roles.length > 0
      ? roles.map((item) => ({
          name: item.name,
          label: item.display_name,
          custom: item.custom,
        }))
      : ROLES.map((name) => ({ name, label: roleLabel(name), custom: false }));
  const defined = choices.filter((item) => item.custom);
  return (
    <>
      <optgroup label="Built in">
        {choices
          .filter((item) => !item.custom)
          .map((item) => (
            <option key={item.name} value={item.name}>
              {item.label}
            </option>
          ))}
      </optgroup>
      {defined.length > 0 ? (
        <optgroup label="Defined here">
          {defined.map((item) => (
            <option key={item.name} value={item.name}>
              {item.label}
            </option>
          ))}
        </optgroup>
      ) : null}
    </>
  );
}

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
  const [roles, setRoles] = useState<Role[]>([]);
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
      const [nextMembers, nextInvitations, nextRoles, me] = await Promise.all([
        listMembers(),
        listInvitations(),
        listRoles(),
        getMe(),
      ]);
      setMembers(nextMembers.items);
      setDefaultLimit(nextMembers.token_limit_default);
      setOversight(nextMembers.chat_admin_access);
      setRoles(nextRoles.items);
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
              placeholder="e.g., name@imperial.ac.uk"
            />
          </Field>
          <Field label="Role">
            <select
              className={`${selectClass} w-full py-2`}
              value={role}
              onChange={(event) => setRole(event.target.value)}
            >
              <RoleOptions roles={roles} />
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

      <RolesPanel
        roles={roles}
        onChanged={async (action, done) => {
          await act(action, done);
        }}
      />

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
                              `${member.email} is now ${labelFor(roles, event.target.value)}.`,
                            )
                          }
                        >
                          <RoleOptions roles={roles} />
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

/**
 * The roles this workspace has, and the form that adds one.
 *
 * A role is a *name for an audience*: the people who may reach a particular
 * body of work. Patented and confidential material does not divide along
 * "researcher" and "reviewer" lines, so the built-in four cannot express who
 * may reach it — a role defined here can, and a rule on a scope then names it
 * once and keeps meaning the same thing as people join and leave.
 *
 * A role carries no permissions of its own. "Based on" says what its members
 * may *do*, taken from a built-in bundle; what they may *reach* is decided on
 * each scope, under Access. Admin is not offered as a base: an admin is let
 * through every rule without one, so a role based on it could never be held
 * back by the mechanism the role exists for.
 */
function RolesPanel({
  roles,
  onChanged,
}: {
  roles: Role[];
  onChanged: (action: () => Promise<unknown>, done: string) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [base, setBase] = useState<string>("researcher");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const defined = roles.filter((role) => role.custom);

  async function add(event: React.FormEvent) {
    event.preventDefault();
    const typed = name.trim();
    if (!typed) return;
    setBusy(true);
    await onChanged(
      () =>
        createRole({
          name: typed,
          display_name: typed,
          description: description.trim(),
          base_role: base,
        }),
      `${typed} is now a role. Nobody holds it and it reaches nothing until you add it to a scope under Access.`,
    );
    setName("");
    setDescription("");
    setBusy(false);
  }

  return (
    <CollapsiblePanel
      title="Roles"
      storageKey="members.roles"
      count={`${defined.length} defined here`}
    >
      <p className="max-w-3xl text-sm text-muted">
        A role names a group of people so a rule can be written about them once. Use one when a
        body of work — patented, confidential, under embargo — should reach some people and not
        others: define the role here, put people in it in the members table, then open the scope
        to it under <strong>Access</strong>. Defining a role grants nothing on its own.
      </p>
      <form className="mt-4 grid gap-3 sm:grid-cols-[1fr_12rem_1fr_auto] sm:items-end" onSubmit={add}>
        <Field label="Role name">
          <Input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="e.g., Patent holders"
          />
        </Field>
        <Field label="Based on">
          <select
            className={`${selectClass} w-full py-2`}
            value={base}
            onChange={(event) => setBase(event.target.value)}
          >
            {BASE_ROLES.map((item) => (
              <option key={item} value={item}>
                {roleLabel(item)}
              </option>
            ))}
          </select>
        </Field>
        <Field label="What it is for">
          <Input
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="e.g., Named on the patent filings"
          />
        </Field>
        <Button type="submit" disabled={busy || !name.trim()}>
          Add role
        </Button>
      </form>

      {defined.length === 0 ? (
        <Empty>
          No roles of your own yet. Everyone holds one of the four built-in ones.
        </Empty>
      ) : (
        <ul className="mt-4 divide-y divide-border">
          {defined.map((role) => (
            <li key={role.id} className="flex flex-wrap items-center gap-3 py-3">
              {editing === role.id ? (
                <RoleEditor
                  role={role}
                  onCancel={() => setEditing(null)}
                  onSave={(changes) => {
                    setEditing(null);
                    void onChanged(
                      () => updateRole(role.id as string, changes),
                      `${changes.display_name} is how the role reads now. Who it reaches is unchanged.`,
                    );
                  }}
                />
              ) : (
                <>
                  <span className="font-medium text-ink">{role.display_name}</span>
                  <code className="rounded bg-surface-subtle px-1.5 py-0.5 text-xs text-muted">
                    {role.name}
                  </code>
                  <span className="text-sm text-muted">
                    {role.members === 1 ? "1 person" : `${role.members} people`}
                  </span>
                  {role.description ? (
                    <span className="text-sm text-muted">· {role.description}</span>
                  ) : null}
                  <label className="ml-auto flex items-center gap-2 text-sm text-muted">
                    Based on
                    <select
                      aria-label={`Based on, for ${role.display_name}`}
                      className={selectClass}
                      value={role.base_role}
                      onChange={(event) =>
                        void onChanged(
                          () => updateRole(role.id as string, { base_role: event.target.value }),
                          `${role.display_name} now does what a ${roleLabel(event.target.value)} does.`,
                        )
                      }
                    >
                      {BASE_ROLES.map((item) => (
                        <option key={item} value={item}>
                          {roleLabel(item)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    type="button"
                    className="text-sm text-link underline hover:no-underline"
                    aria-label={`Change the wording of ${role.name}`}
                    onClick={() => setEditing(role.id as string)}
                  >
                    Change wording
                  </button>
                  {role.members > 0 ? (
                    // No button: the server refuses while anyone is in the role, and
                    // a control that is only going to be refused teaches nothing.
                    <span className="text-xs text-muted">Move everyone out of it to remove it</span>
                  ) : (
                    <DeleteButton
                      what={`the ${role.display_name} role`}
                      consequence="Any rule that named it goes with it, so whatever it opened closes."
                      onDelete={() =>
                        void onChanged(
                          () => deleteRole(role.id as string),
                          `${role.display_name} is gone, with any rule that named it.`,
                        )
                      }
                    />
                  )}
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </CollapsiblePanel>
  );
}

/**
 * Correcting what a role is called and what it is for.
 *
 * The name rules are written about — the one in the grey box — is deliberately
 * not editable. It is stored on every rule that names the role and on every
 * membership that holds it, so changing it here would detach both and leave
 * rules that still look live while matching nobody. Only the reading changes,
 * which is what a correction usually means.
 */
function RoleEditor({
  role,
  onSave,
  onCancel,
}: {
  role: Role;
  onSave: (changes: { display_name: string; description: string }) => void;
  onCancel: () => void;
}) {
  const [displayName, setDisplayName] = useState(role.display_name);
  const [description, setDescription] = useState(role.description);
  const named = displayName.trim();
  return (
    <form
      className="flex w-full flex-wrap items-center gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (named) onSave({ display_name: named, description: description.trim() });
      }}
    >
      <Input
        aria-label={`Shown as, for ${role.name}`}
        className="w-56 py-1"
        value={displayName}
        onChange={(event) => setDisplayName(event.target.value)}
      />
      <Input
        aria-label={`What ${role.name} is for`}
        className="w-72 py-1"
        value={description}
        onChange={(event) => setDescription(event.target.value)}
        placeholder="What it is for"
      />
      <Button type="submit" size="sm" disabled={!named}>
        Save
      </Button>
      <Button type="button" variant="ghost" size="sm" onClick={onCancel}>
        Cancel
      </Button>
      <span className="basis-full text-xs text-muted">
        Rules are written about <code>{role.name}</code>, which stays as it is, so whoever this
        role reaches does not change.
      </span>
    </form>
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
  const [page, setPage] = useState<Page<MemberSession> | null>(null);
  const sessions = page?.items ?? null;
  const [open, setOpen] = useState<(MemberSession & { messages: ChatMessage[] }) | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pager = usePager(20);
  const { cursor, pageSize, reset } = pager;

  // Another member starts again at the first page.
  useEffect(() => {
    setOpen(null);
    reset();
  }, [member.user_id, reset]);

  useEffect(() => {
    let current = true;
    setPage(null);
    listMemberSessions(member.user_id, { after: cursor, limit: pageSize })
      .then((answer) => current && setPage(answer))
      .catch(
        (caught) =>
          current && setError(caught instanceof Error ? caught.message : "chats could not be loaded"),
      );
    return () => {
      current = false;
    };
  }, [member.user_id, cursor, pageSize]);

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
          <div>
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
            {page ? (
              <Pager
                pager={pager}
                shown={page.items.length}
                total={page.total}
                nextCursor={page.next_cursor}
                label="conversations"
              />
            ) : null}
          </div>
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
