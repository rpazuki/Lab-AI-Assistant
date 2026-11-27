/**
 * The email an admin sends with an invitation.
 *
 * There is no mailer: the admin sends it from their own address, which is also
 * what makes it trustworthy to the person receiving it. This is the wording it
 * starts from, filled in with the link or the temporary password. The admin can
 * change the draft on the Members page before copying it.
 */

import type { InvitationKind } from "@/types";

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

const PRODUCT = "Lab AI Assistant";

function expiry(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "long",
    timeStyle: "short",
  });
}

/** An article for a role read aloud: "an admin", "a reviewer". */
function withArticle(role: string): string {
  return /^[aeiou]/i.test(role) ? `an ${role}` : `a ${role}`;
}

export function invitationEmail(facts: InvitationEmailFacts): InvitationEmail {
  const subject = `Your invitation to ${facts.workspace} on the ${PRODUCT}`;
  const opening = [
    "Hello,",
    "",
    `I have invited you to join ${facts.workspace} on the ${PRODUCT} as ${withArticle(facts.role)}. ` +
      "It answers questions from the literature our lab works with, citing the papers it uses.",
    "",
  ];
  const closing = [
    "",
    "If you were not expecting this invitation, you can ignore this email.",
    "",
    "Best wishes,",
    facts.inviter,
  ];
  const steps =
    facts.kind === "temporary_password"
      ? [
          "To join, open the sign-in page:",
          facts.signInUrl,
          "",
          `Sign in with your email address (${facts.email}) and this temporary password:`,
          "",
          `    ${facts.secret}`,
          "",
          `It works once and expires on ${expiry(facts.expiresAt)}. When you sign in with it, ` +
            "you are asked to choose a password of your own, which you use from then on.",
        ]
      : [
          "To accept, open this link and choose a password:",
          facts.secret,
          "",
          `The link works once and expires on ${expiry(facts.expiresAt)}. Please do not ` +
            "forward it: whoever opens it first can use it.",
        ];
  return { subject, body: [...opening, ...steps, ...closing].join("\n") };
}

/** A `mailto:` link that opens the admin's own mail app with the draft filled in. */
export function mailtoLink(to: string, email: InvitationEmail): string {
  return `mailto:${encodeURIComponent(to)}?subject=${encodeURIComponent(
    email.subject,
  )}&body=${encodeURIComponent(email.body)}`;
}
