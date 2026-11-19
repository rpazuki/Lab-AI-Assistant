import { AnalyticsClient } from "@/components/AnalyticsClient";
import { PageHeader } from "@/components/ui/primitives";

export default function AnalyticsPage() {
  return (
    <>
      <PageHeader
        pretitle="Published corpus"
        title="Analytics"
        description="Counts anchored to a release, filtered by the same access policy as retrieval."
      />
      <AnalyticsClient />
    </>
  );
}
