import { InviteClient } from "@/components/InviteClient";

/** The invitation page: the sign-in card's shape, reached from an emailed link. */
export default function InvitePage() {
  return (
    <div className="mx-auto max-w-sm py-10">
      <div className="rounded-card border border-border bg-surface p-8 shadow-card">
        <h1 className="mb-6 text-center text-page-title font-semibold text-heading">
          Accept invitation
        </h1>
        <InviteClient />
      </div>
    </div>
  );
}
