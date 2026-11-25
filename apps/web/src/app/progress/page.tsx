import { ProgressClient } from "@/components/ProgressClient";
import { PageHeader } from "@/components/ui/primitives";

export default function ProgressPage() {
  return (
    <>
      <PageHeader
        help="/help/progress"
        pretitle="Curation"
        title="Collection progress"
        description="How far the collection has got before publication, stage by stage, and why papers are held up."
      />
      <ProgressClient />
    </>
  );
}
