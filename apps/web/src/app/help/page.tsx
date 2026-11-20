import { HelpIndex } from "@/components/HelpClient";
import { PageHeader } from "@/components/ui/primitives";

export default function HelpPage() {
  return (
    <>
      <PageHeader
        pretitle="Help"
        title="How-to guides"
        description="What each page is for, and how to do each thing on it, step by step."
      />
      <HelpIndex />
    </>
  );
}
