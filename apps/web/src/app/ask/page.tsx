import { AskClient } from "@/components/ask/AskClient";
import { PageHeader } from "@/components/ui/primitives";

export default function AskPage() {
  return (
    <>
      <PageHeader
        help="/help/ask"
        pretitle="Published corpus"
        title="Ask"
        description="Question the published corpus. Sources appear as soon as retrieval finishes."
      />
      <AskClient />
    </>
  );
}
