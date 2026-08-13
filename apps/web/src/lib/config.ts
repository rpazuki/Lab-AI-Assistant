/**
 * Where the backend lives.
 *
 * The frontend owns exactly one address, and it comes from the environment the
 * container is given — never from a shared `.env` that a host process might also
 * read. That separation is why `getaddrinfo ENOTFOUND backend` cannot happen here.
 */
export const backendBaseUrl = process.env.LAB_API_URL ?? "http://localhost:8000";

/** Cookie holding the backend session. Never readable from JavaScript. */
export const sessionCookie = "lab_session";

export const cookieOptions = {
  httpOnly: true,
  sameSite: "lax" as const,
  path: "/",
  secure: process.env.NODE_ENV === "production",
};
