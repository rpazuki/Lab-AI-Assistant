import { ReleasesClient } from "@/components/ReleasesClient";
import { PageHeader } from "@/components/ui/primitives";

export default function ReleasesPage() {
  return (
    <>
      <PageHeader
        help="/help/releases"
        pretitle="Publishing"
        title="Releases"
        description="Gate results, serving aliases, and exports for each corpus release."
      />
      <ReleasesClient />
    </>
  );
}
