import { NextResponse } from "next/server";

import { backendBaseUrl, cookieOptions, sessionCookie } from "@/lib/config";

/**
 * Exchange a temporary password for one's own and start a session.
 *
 * The same exchange as sign-in: the token the API returns goes into the
 * `httpOnly` cookie here and never reaches the browser's JavaScript.
 */
export async function POST(request: Request) {
  const body = await request.json();
  const upstream = await fetch(`${backendBaseUrl}/api/v1/auth/first-sign-in`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  const payload = await upstream.json().catch(() => ({}));
  if (!upstream.ok) {
    return NextResponse.json(payload, { status: upstream.status });
  }
  const response = NextResponse.json({ user_id: payload.user_id });
  response.cookies.set(sessionCookie, payload.access_token, {
    ...cookieOptions,
    maxAge: payload.expires_in ?? 3600,
  });
  return response;
}
