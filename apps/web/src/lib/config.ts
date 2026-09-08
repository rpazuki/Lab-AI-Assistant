/**
 * Where the backend lives.
 *
 * The frontend owns exactly one address, and it comes from the environment the
 * container is given — never from a shared `.env` that a host process might also
 * read. That separation is why `getaddrinfo ENOTFOUND backend` cannot happen here.
 */
export const backendBaseUrl = process.env.LAB_API_URL ?? "http://localhost:8000";

/** The public mount owned by this app behind the shared gateway. */
export const appBasePath = normalizeBasePath(
  process.env.APP_BASE_PATH ?? process.env.NEXT_PUBLIC_BASE_PATH ?? "/assist",
);

export function normalizeBasePath(path: string): string {
  if (!path || path === "/") return "";
  return `/${path.replace(/^\/+|\/+$/g, "")}`;
}

export function appPath(path = "/"): string {
  const suffix = path.startsWith("/") ? path : `/${path}`;
  if (suffix === "/") return appBasePath || "/";
  return `${appBasePath}${suffix}`;
}

/** Cookie holding the backend session. Never readable from JavaScript. */
export const sessionCookie = "assist_session";

export const cookieOptions = {
  httpOnly: true,
  sameSite: "lax" as const,
  path: appBasePath || "/",
  secure: process.env.NODE_ENV === "production",
};
