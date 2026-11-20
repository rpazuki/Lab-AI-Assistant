import { notFound } from "next/navigation";

import { HelpArticle } from "@/components/HelpClient";
import { PageHeader } from "@/components/ui/primitives";
import { HELP_TOPICS, helpTopic } from "@/lib/help";

export function generateStaticParams() {
  return HELP_TOPICS.map((topic) => ({ topic: topic.id }));
}

export default async function HelpTopicPage({ params }: { params: Promise<{ topic: string }> }) {
  const topic = helpTopic((await params).topic);
  if (!topic) notFound();
  return (
    <>
      <PageHeader pretitle="How-to guide" title={topic.title} />
      <HelpArticle topic={topic} />
    </>
  );
}
