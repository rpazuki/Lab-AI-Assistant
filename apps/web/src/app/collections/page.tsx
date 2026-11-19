import { CollectionsClient } from "@/components/CollectionsClient";
import { PageHeader } from "@/components/ui/primitives";

export default function CollectionsPage() {
  return (
    <>
      <PageHeader
        pretitle="Curation"
        title="Collections"
        description="Scopes and discovery runs, then candidate decisions, identity conflicts, and the acquisition queue."
      />
      <CollectionsClient />
    </>
  );
}
