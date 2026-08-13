import { backendBaseUrl, sessionCookie } from "@/lib/config";

/**
 * Same-origin proxy to the API.
 *
 * Attaches the session cookie as a bearer token server-side and streams the
 * response through untouched, which matters for server-sent events: buffering the
 * body here would defeat token streaming.
 */
async function proxy(request: Request, path: string[]): Promise<Response> {
  const token = request.headers
    .get("cookie")
    ?.split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${sessionCookie}=`))
    ?.slice(sessionCookie.length + 1);

  const url = new URL(request.url);
  const target = `${backendBaseUrl}/api/v1/${path.join("/")}${url.search}`;

  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  const workspace = request.headers.get("x-workspace");
  if (workspace) headers.set("x-workspace", workspace);
  const requestId = request.headers.get("x-request-id");
  if (requestId) headers.set("x-request-id", requestId);
  if (token) headers.set("authorization", `Bearer ${token}`);

  const upstream = await fetch(target, {
    method: request.method,
    headers,
    body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
    // Required by undici when streaming a request body through.
    duplex: "half",
    cache: "no-store",
  } as RequestInit);

  const responseHeaders = new Headers(upstream.headers);
  responseHeaders.delete("content-encoding");
  responseHeaders.delete("content-length");
  return new Response(upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

export async function GET(request: Request, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await context.params).path);
}
export async function POST(request: Request, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await context.params).path);
}
export async function PATCH(request: Request, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await context.params).path);
}
export async function DELETE(request: Request, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await context.params).path);
}
