import { redirect } from "next/navigation";

/** The first screen after login is the working application, not a landing page. */
export default function Home() {
  redirect("/ask");
}
