import { OperationsClient } from "@/components/OperationsClient";
import { PageHeader } from "@/components/ui/primitives";

export default function OperationsPage() {
  return (
    <>
      <PageHeader
        pretitle="Administration"
        title="Operations"
        description="The task queue, the tasks that need a person or a retry, and the audit log."
      />
      <OperationsClient />
    </>
  );
}
