import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { backendBaseUrl, sessionCookie } from "@/lib/config";
import { HOME_PATH } from "@/lib/navigation";

/**
 * The address people type: the working application for a member with a live
 * session, the sign-in page for everyone else.
 *
 * Asking the API, rather than trusting the cookie's presence, matters here: a
 * token revoked by signing out elsewhere still sits in the browser until it
 * expires, and landing on a page that then fails is worse than signing in.
 */
export default async function Home() {
  const token = (await cookies()).get(sessionCookie)?.value;
  redirect((await sessionIsLive(token)) ? HOME_PATH : "/login");
}

async function sessionIsLive(token: string | undefined): Promise<boolean> {
  if (!token) return false;
  try {
    const response = await fetch(`${backendBaseUrl}/api/v1/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
    return response.ok;
  } catch {
    // An unreachable API is not a session; the sign-in page will say what failed.
    return false;
  }
}
