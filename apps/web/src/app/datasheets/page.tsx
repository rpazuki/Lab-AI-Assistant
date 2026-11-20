import { DatasheetsClient } from "@/components/DatasheetsClient";
import { PageHeader } from "@/components/ui/primitives";

export default function DatasheetsPage() {
  return (
    <>
      <PageHeader
        help="/help/datasheets"
        pretitle="Curation"
        title="Datasheets"
        description="The template that says what to extract, the runs that extract it, and the working sheet of what was proposed."
      />
      <DatasheetsClient />
    </>
  );
}
