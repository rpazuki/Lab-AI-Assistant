import { EvaluationClient } from "@/components/EvaluationClient";
import { PageHeader } from "@/components/ui/primitives";

export default function EvaluationPage() {
  return (
    <>
      <PageHeader
        pretitle="Quality"
        title="Evaluation"
        description="Measure a release against a benchmark: retrieval recall, answer citations, and access-policy leaks."
      />
      <EvaluationClient />
    </>
  );
}
