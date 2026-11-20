import { ReviewClient } from "@/components/review/ReviewClient";
import { PageHeader } from "@/components/ui/primitives";

export default function ReviewPage() {
  return (
    <>
      <PageHeader
        help="/help/review"
        pretitle="Curation"
        title="Review"
        description="Proposed claims with their evidence and the issues validation already found."
      />
      <ReviewClient />
    </>
  );
}
