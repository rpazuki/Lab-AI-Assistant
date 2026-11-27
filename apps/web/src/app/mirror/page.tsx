import { MirrorAccessClient } from "@/components/MirrorAccessClient";
import { PageHeader } from "@/components/ui/primitives";

export default function MirrorAccessPage() {
  return (
    <>
      <PageHeader
        help="/help/mirror"
        pretitle="Administration"
        title="Mirror access"
        description="Which of the publisher's scopes and releases this mirror serves."
      />
      <MirrorAccessClient />
    </>
  );
}
