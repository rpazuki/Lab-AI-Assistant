import { NextResponse } from "next/server";

import { backendBaseUrl, cookieOptions, sessionCookie } from "@/lib/config";

/**
 * Log out.
 *
 * Clearing the cookie is not enough: the API bumps the user's auth epoch so every
 * token already issued stops working. Forgetting a cookie is not a revocation.
 */
export async function POST(request: Request) {
  const token = request.headers
    .get("cookie")
    ?.split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${sessionCookie}=`))
    ?.slice(sessionCookie.length + 1);

  if (token) {
    await fetch(`${backendBaseUrl}/api/v1/auth/logout`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    }).catch(() => undefined);
  }
  const response = new NextResponse(null, { status: 204 });
  response.cookies.set(sessionCookie, "", { ...cookieOptions, maxAge: 0 });
  return response;
}
