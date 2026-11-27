/**
 * The email an admin sends with an invitation.
 *
 * There is no mailer: the admin sends it from their own address, which is also
 * what makes it trustworthy to the person receiving it. Its wording is a
 * template with placeholders such as `{link}`. The workspace's own wording is
 * saved on the Members page, under **Invitation email**. Where none is saved,
 * the product's wording below is used. Either way the admin can still change
 * the draft of each email before sending it.
 */

import type { InvitationKind } from "@/types";

export interface EmailTemplate {
  subject: string;
  body: string;
}

export type EmailTemplates = Record<InvitationKind, EmailTemplate | null>;

export interface InvitationEmailFacts {
  kind: InvitationKind;
  /** The address the invitation is for. */
  email: string;
  /** What the role is called on screen, such as "data steward". */
  role: string;
  /** The workspace's name, such as "RLA Lab". */
  workspace: string;
  /** Who is sending it, as they sign their emails. */
  inviter: string;
  /** For a link: the link. For a temporary password: the password. */
  secret: string;
  /** The sign-in page, for a temporary password. */
  signInUrl: string;
  expiresAt: string;
}

export interface InvitationEmail {
  subject: string;
  body: string;
}

export interface Placeholder {
  name: string;
  /** What it is replaced by, said to the admin writing the template. */
  meaning: string;
  /** The kinds of email it has a value in; both when absent. */
  only?: InvitationKind;
}

export const PLACEHOLDERS: Placeholder[] = [
  { name: "{email}", meaning: "the address the invitation is for" },
  { name: "{role}", meaning: "the role they join as, such as researcher" },
  { name: "{workspace}", meaning: "this workspace's name" },
  { name: "{inviter}", meaning: "your name, or your address if you have not given a name" },
  { name: "{expires}", meaning: "when the invitation runs out, such as 4 December 2026 at 11:04" },
  { name: "{link}", meaning: "the invitation link", only: "link" },
  { name: "{password}", meaning: "the temporary password", only: "temporary_password" },
  { name: "{sign_in_url}", meaning: "the address of the sign-in page", only: "temporary_password" },
];

/** What each kind of email must contain: the thing it is sent to deliver. */
export const REQUIRED_PLACEHOLDER: Record<InvitationKind, string> = {
  link: "{link}",
  temporary_password: "{password}",
};

const PRODUCT = "Lab AI Assistant";

/** The product's wording, used wherever the workspace has not saved its own. */
export const DEFAULT_TEMPLATES: Record<InvitationKind, EmailTemplate> = {
  link: {
    subject: `Your invitation to {workspace} on the ${PRODUCT}`,
    body: [
      "Hello,",
      "",
      `I have invited you to join {workspace} on the ${PRODUCT}, with the role {role}. ` +
        "It answers questions from the literature our lab works with, citing the papers it uses.",
      "",
      "To accept, open this link and choose a password:",
      "{link}",
      "",
      "The link works once and expires on {expires}. Please do not forward it: whoever " +
        "opens it first can use it.",
      "",
      "If you were not expecting this invitation, you can ignore this email.",
      "",
      "Best wishes,",
      "{inviter}",
    ].join("\n"),
  },
  temporary_password: {
    subject: `Your invitation to {workspace} on the ${PRODUCT}`,
    body: [
      "Hello,",
      "",
      `I have invited you to join {workspace} on the ${PRODUCT}, with the role {role}. ` +
        "It answers questions from the literature our lab works with, citing the papers it uses.",
      "",
      "To join, open the sign-in page:",
      "{sign_in_url}",
      "",
      "Sign in with your email address ({email}) and this temporary password:",
      "",
      "    {password}",
      "",
      "It works once and expires on {expires}. When you sign in with it, you are asked to " +
        "choose a password of your own, which you use from then on.",
      "",
      "If you were not expecting this invitation, you can ignore this email.",
      "",
      "Best wishes,",
      "{inviter}",
    ].join("\n"),
  },
};

function expiry(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "long", timeStyle: "short" });
}

/** Each placeholder's value for this invitation. One of another kind is left empty. */
function values(facts: InvitationEmailFacts): Record<string, string> {
  const temporary = facts.kind === "temporary_password";
  return {
    "{email}": facts.email,
    "{role}": facts.role,
    "{workspace}": facts.workspace,
    "{inviter}": facts.inviter,
    "{expires}": expiry(facts.expiresAt),
    "{link}": temporary ? "" : facts.secret,
    "{password}": temporary ? facts.secret : "",
    "{sign_in_url}": facts.signInUrl,
  };
}

/** The template with its placeholders filled in. Text in braces it does not know stays as written. */
export function fillTemplate(template: EmailTemplate, facts: InvitationEmailFacts): InvitationEmail {
  const known = values(facts);
  const fill = (text: string) => text.replace(/\{[a-z_]+\}/g, (name) => known[name] ?? name);
  return { subject: fill(template.subject), body: fill(template.body) };
}

/** The email for an invitation, in the workspace's wording if it saved one. */
export function invitationEmail(
  facts: InvitationEmailFacts,
  saved?: Partial<EmailTemplates> | null,
): InvitationEmail {
  return fillTemplate(saved?.[facts.kind] ?? DEFAULT_TEMPLATES[facts.kind], facts);
}

/**
 * What is wrong with a template before it is saved: the placeholder it cannot
 * do without, and any in braces that nothing fills (a typo, usually), or that
 * belongs to the other kind and would be left empty.
 */
export function templateProblems(kind: InvitationKind, template: EmailTemplate): string[] {
  const problems: string[] = [];
  if (!template.subject.trim()) problems.push("Give it a subject.");
  const required = REQUIRED_PLACEHOLDER[kind];
  if (!template.body.includes(required)) {
    problems.push(
      `The message must contain ${required}, or the person receives no ${
        kind === "link" ? "link" : "temporary password"
      }.`,
    );
  }
  const used = `${template.subject}\n${template.body}`.match(/\{[a-z_]+\}/g) ?? [];
  for (const name of [...new Set(used)]) {
    const placeholder = PLACEHOLDERS.find((item) => item.name === name);
    if (!placeholder) problems.push(`${name} is not a placeholder, so it is sent as written.`);
    else if (placeholder.only && placeholder.only !== kind) {
      problems.push(`${name} has no value in this email, so it is left empty.`);
    }
  }
  return problems;
}

/** Whether a problem stops the template being saved, rather than only warning. */
export function blocksSaving(kind: InvitationKind, template: EmailTemplate): boolean {
  return !template.subject.trim() || !template.body.includes(REQUIRED_PLACEHOLDER[kind]);
}

/** A `mailto:` link that opens the admin's own mail app with the draft filled in. */
export function mailtoLink(to: string, email: InvitationEmail): string {
  return `mailto:${encodeURIComponent(to)}?subject=${encodeURIComponent(
    email.subject,
  )}&body=${encodeURIComponent(email.body)}`;
}
