import { NextResponse, type NextRequest } from "next/server";

import { sessionCookie } from "@/lib/config";
import { PUBLIC_PATHS } from "@/lib/navigation";

/**
 * Send a visitor without a session to the sign-in page, remembering where they
 * were going.
 *
 * Only the cookie's presence is checked here: this is routing, not
 * authentication. A present but revoked or expired token still reaches the page,
 * whose API calls the backend refuses; the root page goes further and asks the
 * API, so the address people type always lands on the right screen.
 *
 * The target is built from the request's own URL, and Next sends a same-origin
 * redirect as a relative Location. That matters behind the gateway: an absolute
 * URL naming the loopback port this server listens on would send the browser
 * there.
 */
export function middleware(request: NextRequest) {
  // nextUrl.pathname is relative to the basePath, like every href in this app.
  const { pathname, search } = request.nextUrl;
  if (PUBLIC_PATHS.includes(pathname) || request.cookies.has(sessionCookie)) {
    return NextResponse.next();
  }
  // The clone keeps the basePath, so this is /assist/login.
  const target = request.nextUrl.clone();
  target.pathname = "/login";
  target.search = pathname === "/" ? "" : `?next=${encodeURIComponent(`${pathname}${search}`)}`;
  return NextResponse.redirect(target, 307);
}

export const config = {
  // Pages only: route handlers answer 401 themselves, and assets need no session.
  matcher: ["/((?!api/|_next/|favicon\\.ico|.*\\.[a-z0-9]+$).*)"],
};
