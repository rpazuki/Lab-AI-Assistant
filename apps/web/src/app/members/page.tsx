import { MembersClient } from "@/components/MembersClient";
import { PageHeader } from "@/components/ui/primitives";

export default function MembersPage() {
  return (
    <>
      <PageHeader
        pretitle="Administration"
        title="Members"
        description="Who belongs to this workspace, in which role, and the invitations that bring new members in."
      />
      <MembersClient />
    </>
  );
}
