import { AnswerReviewsClient } from "@/components/AnswerReviewsClient";
import { PageHeader } from "@/components/ui/primitives";

export default function AnswerReviewsPage() {
  return (
    <>
      <PageHeader
        help="/help/answer-reviews"
        pretitle="Quality"
        title="Answer reviews"
        description="Answers from evaluation runs, assigned to you to grade. Your grades sit beside the automatic metrics."
      />
      <AnswerReviewsClient />
    </>
  );
}
